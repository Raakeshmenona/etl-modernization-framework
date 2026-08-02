#!/usr/bin/perl
#
# LEGACY: derive regulatory account status and pricing tier.
# usage: perl account_status_derive.pl accounts.csv > out.csv
#
# The branch order below IS the business rule. It was last signed off by
# Risk in 2011 and the ordering of the dormancy check against the zero
# balance check is deliberate.
use strict;
use warnings;

use constant REF_DATE      => '2026-07-01';
use constant DORMANT_DAYS  => 90;

my $file = shift or die "usage: account_status_derive.pl accounts.csv\n";

# Days between two YYYY-MM-DD dates without pulling in Date::Calc.
sub days_between {
    my ($from, $to) = @_;
    return _epoch_days($to) - _epoch_days($from);
}

sub _epoch_days {
    my ($iso) = @_;
    my ($y, $m, $d) = split(/-/, $iso, -1);
    # Howard Hinnant's civil-from-days, inverted. Integer only.
    $y -= ($m <= 2) ? 1 : 0;
    my $era = int($y / 400);
    my $yoe = $y - $era * 400;
    my $doy = int((153 * ($m + (($m > 2) ? -3 : 9)) + 2) / 5) + $d - 1;
    my $doe = $yoe * 365 + int($yoe / 4) - int($yoe / 100) + $doy;
    return $era * 146097 + $doe - 719468;
}

open(my $fh, '<', $file) or die "cannot open $file: $!";
my $header = <$fh>;

print "account_id,status,tier,days_since_activity\n";

while (my $line = <$fh>) {
    chomp $line;
    next unless length $line;

    my @f = split(/,/, $line, -1);
    my ($account_id, $cust_id, $balance, $last_activity, $product, $overdraft) = @f[0 .. 5];

    my $days = days_between($last_activity, REF_DATE);

    my ($status, $tier);

    if ($balance < 0) {
        # String comparison on the flag, not numeric: 'Y' == 0 is true in
        # Perl but 'Y' eq 'Y' is what we mean.
        $status = ($overdraft eq 'Y') ? 'OVERDRAWN_AUTHORISED' : 'OVERDRAWN_UNAUTHORISED';
        $tier   = 'NONE';
    }
    elsif ($balance == 0) {
        unless ($days <= DORMANT_DAYS) {
            $status = 'DORMANT';
        }
        else {
            $status = 'ACTIVE_ZERO';
        }
        $tier = 'NONE';
    }
    else {
        $status = ($days > 365) ? 'DORMANT' : 'ACTIVE';
        if    ($balance < 1000)   { $tier = 'STANDARD' }
        elsif ($balance < 10000)  { $tier = 'PREMIUM' }
        else                      { $tier = 'PRIVATE' }
    }

    printf("%s,%s,%s,%d\n", $account_id, $status, $tier, $days);
}
close($fh);
