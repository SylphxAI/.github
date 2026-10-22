# Shared scanner for check.sh. Reads env: TERMS (tsv), MODE (warn|fail), SCAN (diff|tree),
# BASELINE, WRITE_BASELINE (init|shrink|empty), TODAY, BARE_UNTIL.
use strict; use warnings;
my $mode  = $ENV{MODE} || "fail";
my $level = $mode eq "warn" ? "warning" : "error";
my $today = $ENV{TODAY} || do { my @t = gmtime; sprintf("%04d-%02d-%02d", $t[5] + 1900, $t[4] + 1, $t[3]) };
my $bare_until = $ENV{BARE_UNTIL} || "2026-10-15";

open(my $fh, "<", $ENV{TERMS}) or die "terms: $!";
my @t;
while (<$fh>) { chomp; next if /^\s*(#|$)/; my ($p, $r) = split /\t/, $_, 2; push @t, [qr/$p/i, $r]; }

# Returns "" when the line has no marker, "ok" when it is allowed, else an error message.
my %warned;
sub marker {
  my ($l, $file, $n) = @_;
  if ($l =~ /plain-language: allow\(([^)]*)\)/) {
    my $body = $1;
    my ($reason, $until) = $body =~ /^(.*?)\s*;\s*until=(\d{4}-\d{2}-\d{2})\s*$/ ? ($1, $2) : ("", "");
    return "allow marker needs 'allow(<reason>; until=YYYY-MM-DD)'" unless $until;
    return "allow marker needs a reason before '; until='" unless $reason =~ /\S/;
    return "allow marker expired on $until; fix the line or renew with a new reason and date" if $until lt $today;
    return "ok";
  }
  if ($l =~ /plain-language: allow/) {
    return "bare 'plain-language: allow' is no longer accepted (grace ended $bare_until); use allow(<reason>; until=YYYY-MM-DD)" if $today gt $bare_until;
    print "::warning file=${file},line=${n}::bare 'plain-language: allow' works only until $bare_until; use allow(<reason>; until=YYYY-MM-DD)\n";
    return "ok";
  }
  return "";
}

# Hits on one line: list of lowercased matched names; [] when allowed.
sub hits {
  my ($l, $file, $n) = @_;
  my @h;
  for my $x (@t) { while ($l =~ /$x->[0]/g) { push @h, [lc $&, $x->[1]] } }
  return () unless @h;
  my $m = marker($l, $file, $n);
  return () if $m eq "ok";
  print "::${level} file=${file},line=${n}::$m\n" if $m;
  return @h;
}

if (($ENV{SCAN} || "diff") eq "diff") {
  my ($file, $line, $found) = ("", 0, 0);
  while (<STDIN>) {
    if (/^\+\+\+ b\/(.*)/) { $file = $1; next }
    if (/^@@ -\S+ \+(\d+)/) { $line = $1; next }
    if (s/^\+//) {
      for my $h (hits($_, $file, $line)) { print "::${level} file=${file},line=${line}::\"$h->[0]\": $h->[1]\n"; $found++ }
      $line++;
    }
  }
  if ($found) { print "$found listed term(s) on added lines.\n"; exit($mode eq "warn" ? 0 : 1) }
  print "No listed terms on added lines.\n";
  exit 0;
}

# Tree ratchet: per (path, name) counts on every tracked file against the committed baseline.
local $/ = "\0";
my (%count, %first, %msg);
while (my $f = <STDIN>) {
  chomp $f;
  next if -l $f or !-f $f;
  open(my $in, "<:raw", $f) or next;
  my $head = ""; read($in, $head, 8000); next if $head =~ /\0/;
  seek($in, 0, 0);
  local $/ = "\n";
  my $n = 0;
  while (my $l = <$in>) {
    $n++;
    for my $h (hits($l, $f, $n)) { my $k = "$f\t$h->[0]"; $count{$k}++; $first{$k} //= $n; $msg{$k} = $h->[1] }
  }
}
my %base;
my $bf = $ENV{BASELINE};
if (-f $bf) {
  open(my $b, "<", $bf) or die "baseline: $!";
  local $/ = "\n";  # the file list above is NUL-separated; the baseline is line-based
  while (<$b>) { chomp; next if /^\s*(#|$)/; my ($p, $nm, $c) = split /\t/; $base{"$p\t$nm"} = $c; }
}
my $w = $ENV{WRITE_BASELINE} || "";
if ($w) {
  my %out = %count;
  if ($w eq "shrink") {
    my $grow = 0;
    for my $k (keys %count) { if (!exists $base{$k} or $count{$k} > $base{$k}) { print STDERR "cannot shrink: new or grown hit $k\n"; $grow++ } }
    exit 1 if $grow;
  }
  my $total = 0; $total += $_ for values %out;
  print "# plain-language baseline: path, name, count. It only shrinks: new hits fail, removed hits leave this file.\n";
  print "# total $total\n";
  print "$_\t$out{$_}\n" for sort keys %out;
  exit 0;
}
my ($bad, $total) = (0, 0);
for my $k (sort keys %count) {
  $total += $count{$k};
  my ($p, $nm) = split /\t/, $k;
  my $b = $base{$k} // 0;
  if ($count{$k} > $b) {
    print "::${level} file=${p},line=$first{$k}::\"$nm\" appears $count{$k} time(s), baseline allows $b: $msg{$k}\n"; $bad++;
  }
}
for my $k (sort keys %base) {
  my $c = $count{$k} // 0;
  if ($c < $base{$k}) {
    my ($p, $nm) = split /\t/, $k;
    print "::${level} file=${bf}::baseline lists $base{$k} \"$nm\" hit(s) in $p but $c remain; shrink the baseline (check.sh with PLAIN_LANGUAGE_WRITE_BASELINE=shrink)\n"; $bad++;
  }
}
print "Product-name ratchet: $total hit(s) remain against the baseline; $bad violation(s).\n";
exit($bad && $mode ne "warn" ? 1 : 0);
