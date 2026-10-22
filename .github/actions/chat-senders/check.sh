#!/usr/bin/env bash
# Report chat-API hosts on lines a change adds outside the allow-list.
# Usage: check.sh <git-range> <allow-list.tsv>. CHECK_REPOSITORY names the
# calling repository (owner/name); CHAT_SENDERS_MODE is fail (default) or warn.
set -euo pipefail
export LC_ALL=C
range="$1"; allow="$2"
git diff --unified=0 --no-color --no-ext-diff "$range" -- . \
| ALLOW="$allow" MODE="${CHAT_SENDERS_MODE:-fail}" REPO="${CHECK_REPOSITORY:-}" perl -e '
  # The hosts a direct chat sender calls (Telegram Bot API, Slack incoming
  # webhooks and chat.postMessage, Discord webhooks).
  my $hosts = qr{api\.telegram\.org|hooks\.slack\.com|slack\.com/api/chat\.postMessage|discord(?:app)?\.com/api/webhooks}i;
  open(my $fh, "<", $ENV{ALLOW}) or die "allow-list: $!";
  my @allow;
  while (<$fh>) {
    chomp; next if /^\s*(#|$)/;
    my ($repo, $path) = split /\t/, $_, 3;
    die "allow-list row needs a repository and a path: $_\n" unless defined $path && length $path;
    push @allow, [$repo, qr/$path/];
  }
  sub allowed {
    my ($file) = @_;
    for my $a (@allow) {
      next unless $a->[0] eq "*" || $a->[0] eq $ENV{REPO};
      return 1 if $file =~ $a->[1];
    }
    return 0;
  }
  my ($file, $line, $found) = ("", 0, 0);
  my %ok;
  while (<STDIN>) {
    if (/^\+\+\+ (?:b\/(.*)|\/dev\/null)/) { $file = $1 // ""; next }
    if (/^@@ -\S+ \+(\d+)/) { $line = $1; next }
    next unless s/^\+//;
    if (/($hosts)/) {
      $ok{$file} //= allowed($file);
      unless ($ok{$file}) {
        my $level = $ENV{MODE} eq "warn" ? "warning" : "error";
        print "::${level} file=${file},line=${line}::chat-senders: $1 is a direct chat sender; send through Notify (cloud ADR notify-chat-channel), or add the path to the allow-list in SylphxAI/.github if it is chat administration, a conversational agent or platform paging\n";
        $found++;
      }
    }
    $line++;
  }
  if ($found) { print "$found direct chat-API host(s) on added lines outside the allow-list.\n"; exit($ENV{MODE} eq "warn" ? 0 : 1) }
  print "No direct chat-API hosts on added lines outside the allow-list.\n";
'
