#!/usr/bin/env bash
# Report Simplified-only characters on zh-Hant lines a change adds.
# Usage: check.sh <git-range> <simplified.tsv>. A line containing "zh-hant: allow" is skipped.
set -euo pipefail
range="$1"; table="$2"
git -c core.quotePath=false diff --unified=0 --no-color "$range" -- . \
  ':(exclude)*.lock' ':(exclude)*package-lock.json' ':(exclude)*/vendor/*' \
  ':(exclude)*/node_modules/*' ':(exclude)*.generated.*' ':(exclude)*/simplified.tsv' \
| TABLE="$table" perl -CSD -Mutf8 -e '
  open(my $fh, "<:encoding(UTF-8)", $ENV{TABLE}) or die "table: $!";
  my %s;
  while (<$fh>) { chomp; next if /^\s*(#|$)/; my ($c, $t) = split /\t/, $_, 2; $s{$c} = $t; }
  my $cls = join "", map { quotemeta } keys %s;
  my $re = qr/([$cls])/;
  # zh-Hant files and directories: zh_TW, zh-HK, zh-Hant, zhTW, values-zh-rTW, zh-Hant.lproj.
  my $hant_path = qr{(?:^|[/._-])(?:zh[-_]?(?:r?(?:tw|hk|mo)|hant(?:[-_](?:tw|hk|mo))?))(?:[/._-]|$)}i;
  # A zh-Hant key in a multi-locale file: "zh_tw": "...", zh-HK = ..., zhHant: ...
  my $hant_key = qr{(?<![a-z])zh[-_]?(?:tw|hk|mo|hant(?:[-_](?:tw|hk|mo))?)(?![a-z])["\x27]?\s*[:=]\s*(.*)}i;
  # The value ends where a Simplified-locale key starts on the same line.
  my $hans_key = qr{(?<![a-z])zh[-_]?(?:cn|sg|hans)(?![a-z])}i;
  my ($file, $line, $found, $hant) = ("", 0, 0, 0);
  while (<STDIN>) {
    if (/^\+\+\+ (?:b\/)?(.*)/) { $file = $1; $hant = ($file =~ $hant_path) ? 1 : 0; next }
    if (/^@@ -\S+ \+(\d+)/) { $line = $1; next }
    next unless s/^\+//;
    unless (/zh-hant: allow/) {
      my $text = $hant ? $_ : (/$hant_key/ ? $1 : "");
      $text =~ s/$hans_key.*//s unless $hant;
      my %seen;
      while ($text =~ /$re/g) {
        my $c = $1; next if $seen{$c}++;
        print "::error file=${file},line=${line}::\"$c\" is a Simplified character in zh-Hant content; write \"$s{$c}\" (owner standards/experience.md)\n";
        $found++;
      }
    }
    $line++;
  }
  if ($found) { print "$found Simplified character(s) on added zh-Hant lines.\n"; exit 1 }
  print "No Simplified characters on added zh-Hant lines.\n";
'
