#!/usr/bin/env bash
# Report listed terms on lines a change adds. Usage: check.sh <git-range> <terms.tsv> [pathspec...]
# A line containing "plain-language: allow" is skipped; extra pathspecs narrow or exclude paths.
set -euo pipefail
export LC_ALL=C
range="$1"; terms="$2"; shift 2
git diff --unified=0 --no-color "$range" -- . \
  ':(exclude)*.lock' ':(exclude)*package-lock.json' ':(exclude)*/vendor/*' \
  ':(exclude)*/node_modules/*' ':(exclude)*.generated.*' ':(exclude)*.tsv' "$@" \
| TERMS="$terms" MODE="${PLAIN_LANGUAGE_MODE:-fail}" perl -e '
  open(my $fh, "<", $ENV{TERMS}) or die "terms: $!";
  my @t;
  while (<$fh>) { chomp; next if /^\s*(#|$)/; my ($p, $r) = split /\t/, $_, 2; push @t, [qr/$p/i, $r]; }
  my ($file, $line, $found) = ("", 0, 0);
  while (<STDIN>) {
    if (/^\+\+\+ b\/(.*)/) { $file = $1; next }
    if (/^@@ -\S+ \+(\d+)/) { $line = $1; next }
    next unless s/^\+//;
    unless (/plain-language: allow/) {
      for my $x (@t) {
        if ($_ =~ $x->[0]) {
          my $level = $ENV{MODE} eq "warn" ? "warning" : "error";
          print "::${level} file=${file},line=${line}::\"$&\": $x->[1]\n";
          $found++;
        }
      }
    }
    $line++;
  }
  if ($found) { print "$found listed term(s) on added lines.\n"; exit($ENV{MODE} eq "warn" ? 0 : 1) }
  print "No listed terms on added lines.\n";
'
