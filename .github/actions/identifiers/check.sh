#!/usr/bin/env bash
# Report forbidden id generators on lines a change adds. Usage: check.sh <git-range> <rules.tsv> [pathspec...]
# A line containing "identifiers: allow" is skipped; extra pathspecs narrow or exclude paths.
set -euo pipefail
export LC_ALL=C
range="$1"; rules="$2"; shift 2
git diff --unified=0 --no-color "$range" -- . \
  ':(exclude)*.lock' ':(exclude)*package-lock.json' ':(exclude)*/vendor/*' \
  ':(exclude)*/node_modules/*' ':(exclude)*.generated.*' ':(exclude)*.tsv' "$@" \
| RULES="$rules" MODE="${IDENTIFIERS_MODE:-fail}" perl -e '
  open(my $fh, "<", $ENV{RULES}) or die "rules: $!";
  my @r;
  while (<$fh>) { chomp; next if /^\s*(#|$)/; my ($ext, $p, $m) = split /\t/, $_, 3; push @r, [qr/$ext/, qr/$p/, $m]; }
  my ($file, $line, $found) = ("", 0, 0);
  while (<STDIN>) {
    if (/^\+\+\+ b\/(.*)/) { $file = $1; next }
    if (/^@@ -\S+ \+(\d+)/) { $line = $1; next }
    next unless s/^\+//;
    next if /identifiers: allow/;
    for my $x (@r) {
      next unless $file =~ $x->[0];
      if ($_ =~ $x->[1]) {
        my $level = $ENV{MODE} eq "warn" ? "warning" : "error";
        print "::${level} file=${file},line=${line}::identifiers: $x->[2] - see owner standards/identifiers.md\n";
        $found++;
      }
    }
    $line++;
  }
  if ($found) { print "$found non-v7 identifier(s) on added lines.\n"; exit($ENV{MODE} eq "warn" ? 0 : 1) }
  print "No non-v7 identifiers on added lines.\n";
'
