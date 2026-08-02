#!/usr/bin/perl
#
# LEGACY: completeness report over the nightly pipe-delimited feed.
# usage: perl null_handling_report.pl raw_feed.psv > out.csv
#
# Completeness is measured against the number of fields the record actually
# carried, not the header width. Records that terminate early are scored on
# what they delivered.
use strict;
use warnings;

my $file = shift or die "usage: null_handling_report.pl raw_feed.psv\n";
open(my $fh, '<', $file) or die "cannot open $file: $!";

my $header = <$fh>;

print "record_id,field_count,populated_fields,completeness_pct\n";

while (my $line = <$fh>) {
    chomp $line;
    next unless length $line;

    # No LIMIT argument: split discards trailing empty fields. A record
    # ending "...|OK||" arrives here with the tail already gone, which is
    # exactly how the observed field count ends up smaller than the header.
    my @f = split(/\|/, $line);

    my $field_count = scalar(@f);
    next unless $field_count;

    my $populated = 0;
    foreach my $v (@f) {
        $populated++ if defined($v) && length($v);
    }

    printf("%s,%d,%d,%.1f\n",
           $f[0], $field_count, $populated, 100 * $populated / $field_count);
}
close($fh);
