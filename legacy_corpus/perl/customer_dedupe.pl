#!/usr/bin/perl
#
# LEGACY: nightly customer de-duplication (circa 2009, MDM feed prep).
# usage: perl customer_dedupe.pl customers.csv > out.csv
#
# Collapses records that share a normalised (LAST, FIRST, DOB) key, keeping
# the first customer_id seen and counting how many later rows folded into it.
use strict;
use warnings;

my $file = shift or die "usage: customer_dedupe.pl customers.csv\n";
open(my $fh, '<', $file) or die "cannot open $file: $!";

my $header = <$fh>;

my %first_id_for_key;   # normalised key -> surviving customer_id
my %dup_count;          # surviving customer_id -> rows folded in
my %row_for_id;         # surviving customer_id -> emitted prefix

while (my $line = <$fh>) {
    chomp $line;
    next unless length $line;

    # -1 keeps trailing empty fields. Omitting it is the classic defect;
    # see null_handling_report.pl for what happens when you forget.
    my @f = split(/,/, $line, -1);
    my ($id, $first, $last, $dob) = @f[0 .. 3];

    for ($first, $last) {
        s/^\s+//;
        s/\s+$//;
        s/\s+/ /g;
        $_ = uc $_;
    }

    my $key = join('|', $last, $first, $dob);

    if (exists $first_id_for_key{$key}) {
        $dup_count{ $first_id_for_key{$key} }++;
        next;
    }

    $first_id_for_key{$key} = $id;
    $row_for_id{$id} = join(',', $id, "$last $first", $dob);
}
close($fh);

print "customer_id,norm_name,dob,dup_count\n";

# NOTE: Perl's default sort is a STRING comparison, so ids come out
# 1, 10, 100, 101, ... 2, 20, ... not in numeric order. Downstream
# reconciliation has depended on this ordering for fifteen years.
foreach my $id (sort keys %row_for_id) {
    print $row_for_id{$id} . "," . ($dup_count{$id} || 0) . "\n";
}
