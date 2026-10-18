#!/usr/bin/env bash
# Report a newly introduced sleep or polling loop on an event-driven primary path.
# Usage: check.sh <git-range> <rules.tsv> [pathspec...]
# Only lines the range adds are checked, so a repository adopts the rule without
# first rewriting the loops it already ships. A line that names the event it
# waits for, or a bounded resync or recovery, carries
# `standards-lint: allow(<reason>)` and is not a finding.
# STANDARDS_LINT_MODE is fail (default) or warn.
set -euo pipefail
export LC_ALL=C
range="$1"; rules="$2"; shift 2
git diff --unified=0 --no-color --no-ext-diff "$range" -- . \
  ':(exclude)*.lock' ':(exclude)*package-lock.json' ':(exclude)*/vendor/*' \
  ':(exclude)*/node_modules/*' ':(exclude)*.generated.*' ':(exclude)*.tsv' \
  "$@" \
| RULES="$rules" MODE="${STANDARDS_LINT_MODE:-fail}" perl -e '
  open(my $fh, "<", $ENV{RULES}) or die "rules: $!";
  my @rules;
  while (<$fh>) {
    chomp; next if /^\s*(#|$)/;
    my ($ext, $pattern, $message, $code) = split /\t/, $_, 4;
    die "rules row needs four columns: $_\n" unless defined $code && length $code;
    push @rules, [qr/$ext/, qr/$pattern/, $message, $code];
  }
  die "rules: no rows\n" unless @rules;
  my ($file, $start, $found) = ("", 0, 0);
  my @added;
  # A loop and the read it polls with are usually on different lines, and a
  # zero-context diff keeps them apart. Judge each added hunk as one block, and
  # report the line the match ends on.
  sub judge {
    my ($file, $start, $added) = @_;
    return 0 unless @$added;
    my $block = join("", @$added);
    my $hits = 0;
    for my $rule (@rules) {
      next unless $file =~ $rule->[0];
      while ($block =~ /$rule->[1]/g) {
        # Report the line the match ends on, which is the read or the sleep,
        # not the line the loop opened on.
        my $at = $start + scalar(() = substr($block, 0, $+[0]) =~ /\n/g);
        # A documented exception names why this line is not the primary path.
        next if $added->[$at - $start] =~ /standards-lint:\s*allow\([^()\n]+\)/;
        my ($message, $code) = ($rule->[2], $rule->[3]);
        my $level = $ENV{MODE} eq "warn" ? "warning" : "error";
        print "::${level} file=${file},line=${at},title=standards-lint/${code}::standards-lint: ${message} [${code}]\n";
        $hits++;
      }
    }
    return $hits;
  }
  while (<STDIN>) {
    if (/^\+\+\+ (?:b\/(.*)|\/dev\/null)/) {
      $found += judge($file, $start, \@added);
      ($file, @added) = ($1 // "");
      next;
    }
    if (/^@@ -\S+ \+(\d+)/) {
      $found += judge($file, $start, \@added);
      ($start, @added) = ($1);
      next;
    }
    next unless s/^\+//;
    push @added, $_;
  }
  $found += judge($file, $start, \@added);
  if ($found) {
    print "$found standards-lint finding(s) on added lines.\n";
    exit($ENV{MODE} eq "warn" ? 0 : 1);
  }
  print "No standards-lint findings on added lines.\n";
'
