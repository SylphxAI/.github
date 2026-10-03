#!/usr/bin/env bash
# Report forbidden id generators on lines a change adds. Usage: check.sh <git-range> <rules.tsv> [pathspec...]
# A line containing "identifiers: allow" is skipped; extra pathspecs narrow or exclude paths.
set -euo pipefail
export LC_ALL=C
range="$1"; rules="$2"; shift 2
action_dir="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
head_rev="$(git rev-parse --verify "${range##*..}^{commit}")"
git diff --unified=0 --no-color "$range" -- . \
  ':(exclude)*.lock' ':(exclude)*package-lock.json' ':(exclude)*/vendor/*' \
  ':(exclude)*/node_modules/*' ':(exclude)*.generated.*' ':(exclude)*.tsv' "$@" \
| RULES="$rules" MODE="${IDENTIFIERS_MODE:-fail}" HEAD_REV="$head_rev" \
  ALLOWANCES="$action_dir/historical-allowances.json" perl -e '
  use Digest::SHA qw(sha256_hex);
  use JSON::PP qw(decode_json);
  open(my $fh, "<", $ENV{RULES}) or die "rules: $!";
  my @r;
  while (<$fh>) { chomp; next if /^\s*(#|$)/; my ($ext, $p, $m, $code) = split /\t/, $_, 4; push @r, [qr/$ext/, qr/$p/, $m, $code]; }
  # Only the pinned action owns allowances; never consult the caller checkout,
  # an input or an environment-supplied list. Hash the diff endpoint git blob,
  # not the working tree (which may contain uncommitted or generated changes).
  open(my $af, "<", $ENV{ALLOWANCES}) or die "historical allowances: $!";
  my $allowances = decode_json(do { local $/; <$af> });
  my %hashes;
  sub historical_allowance {
    my ($file, $code) = @_;
    for my $a (@$allowances) {
      next unless $a->{repository} eq ($ENV{CHECK_REPOSITORY} // "")
        && $a->{path} eq $file && $a->{code} eq $code;
      unless (exists $hashes{$file}) {
        open(my $blob, "-|", "git", "show", "$ENV{HEAD_REV}:$file") or die "git blob: $!";
        binmode $blob;
        my $bytes = do { local $/; <$blob> };
        close($blob) or die "git blob read failed: $file";
        $hashes{$file} = sha256_hex($bytes);
      }
      return 1 if $hashes{$file} eq $a->{sha256};
    }
    return 0;
  }
  my ($file, $line, $found) = ("", 0, 0);
  while (<STDIN>) {
    if (/^\+\+\+ b\/(.*)/) { $file = $1; next }
    if (/^@@ -\S+ \+(\d+)/) { $line = $1; next }
    next unless s/^\+//;
    next if /identifiers: allow/;
    for my $x (@r) {
      next unless $file =~ $x->[0];
      if ($_ =~ $x->[1]) {
        next if historical_allowance($file, $x->[3]);
        my $level = $ENV{MODE} eq "warn" ? "warning" : "error";
        print "::${level} file=${file},line=${line}::identifiers: $x->[2] - see owner standards/identifiers.md [$x->[3]]\n";
        $found++;
      }
    }
    $line++;
  }
  if ($found) { print "$found non-v7 identifier(s) on added lines.\n"; exit($ENV{MODE} eq "warn" ? 0 : 1) }
  print "No non-v7 identifiers on added lines.\n";
'
