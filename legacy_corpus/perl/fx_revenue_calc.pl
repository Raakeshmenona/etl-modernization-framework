#!/usr/bin/perl
#
# LEGACY: FX commission revenue by channel and currency.
# usage: perl fx_revenue_calc.pl transactions.csv fx_rates.csv > out.csv
#
# Commission is 1.5% of the USD-converted notional. Currencies with no rate
# on file convert at Perl's undef-in-numeric-context value, i.e. zero, and
# therefore contribute nothing to revenue. That behaviour is load-bearing:
# treasury reconciles to it monthly.
use strict;
use warnings;

my $txn_file = shift or die "usage: fx_revenue_calc.pl transactions.csv fx_rates.csv\n";
my $fx_file  = shift or die "usage: fx_revenue_calc.pl transactions.csv fx_rates.csv\n";

my $COMMISSION_BPS = 0.015;

my %rate;
open(my $fx, '<', $fx_file) or die "cannot open $fx_file: $!";
my $fx_header = <$fx>;
while (my $line = <$fx>) {
    chomp $line;
    next unless length $line;
    my ($currency, $r) = split(/,/, $line, -1);
    $rate{$currency} = $r;
}
close($fx);

my (%usd, %commission, %count);

open(my $fh, '<', $txn_file) or die "cannot open $txn_file: $!";
my $header = <$fh>;
while (my $line = <$fh>) {
    chomp $line;
    next unless length $line;

    my @f = split(/,/, $line, -1);
    my ($txn_id, $cust_id, $txn_date, $amount, $currency, $channel) = @f[0 .. 5];

    # No warnings pragma disabled here on purpose: an unrated currency yields
    # undef, which numifies to 0 and zeroes out the row.
    no warnings 'uninitialized';
    my $converted = $amount * $rate{$currency};

    my $key = join('|', $channel, $currency);
    $usd{$key}        += $converted;
    $commission{$key} += $converted * $COMMISSION_BPS;
    $count{$key}++;
}
close($fh);

print "channel,currency,txn_count,total_usd,total_commission\n";
foreach my $key (sort keys %usd) {
    my ($channel, $currency) = split(/\|/, $key, -1);
    printf("%s,%s,%d,%.2f,%.2f\n",
           $channel, $currency, $count{$key}, $usd{$key}, $commission{$key});
}
