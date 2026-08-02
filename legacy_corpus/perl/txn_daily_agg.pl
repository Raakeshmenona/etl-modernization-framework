#!/usr/bin/perl
#
# LEGACY: daily transaction rollup by settlement date and currency.
# usage: perl txn_daily_agg.pl transactions.csv > out.csv
#
# Source dates arrive as DD/MM/YYYY from the European clearing feed.
use strict;
use warnings;

my $file = shift or die "usage: txn_daily_agg.pl transactions.csv\n";
open(my $fh, '<', $file) or die "cannot open $file: $!";

my $header = <$fh>;

my (%amount, %count);

while (my $line = <$fh>) {
    chomp $line;
    next unless length $line;

    my @f = split(/,/, $line, -1);
    my ($txn_id, $cust_id, $txn_date, $amount, $currency, $channel) = @f[0 .. 5];

    # DD/MM/YYYY -> YYYY-MM-DD. Reading this as MM/DD/YYYY silently swaps
    # day and month for every row where both are <= 12.
    my ($dd, $mm, $yyyy) = split(/\//, $txn_date, -1);
    my $iso = sprintf("%04d-%02d-%02d", $yyyy, $mm, $dd);

    my $key = join('|', $iso, $currency);
    $amount{$key} += $amount;
    $count{$key}++;
}
close($fh);

print "settlement_date,currency,txn_count,total_amount\n";
foreach my $key (sort keys %amount) {
    my ($iso, $currency) = split(/\|/, $key, -1);
    printf("%s,%s,%d,%.2f\n", $iso, $currency, $count{$key}, $amount{$key});
}
