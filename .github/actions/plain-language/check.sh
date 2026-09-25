#!/usr/bin/env bash
# Report coined terms on lines a change adds. Usage: check.sh <git-range> <terms.tsv>
# A line containing "plain-language: allow" is skipped.
set -euo pipefail
export LC_ALL=C
range="$1"; terms="$2"
git diff --unified=0 --no-color "$range" -- . \
  ':(exclude)*.lock' ':(exclude)*package-lock.json' ':(exclude)*/vendor/*' \
  ':(exclude)*/node_modules/*' ':(exclude)*.generated.*' ':(exclude)*/terms.tsv' \
| TERMS="$terms" perl -e '
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
          print "::error file=${file},line=${line}::\"$&\" is coined jargon; write \"$x->[1]\" (owner standards/docs.md, Plain language)\n";
          $found++;
        }
      }
    }
    $line++;
  }
  if ($found) { print "$found coined term(s) on added lines.\n"; exit 1 }
  print "No coined terms on added lines.\n";
'
