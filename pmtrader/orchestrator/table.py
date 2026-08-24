"""Creating `pmt-fleet`, and the two guards that go on it.

Sizing, honestly
----------------
Two nodes beating every 30s is 5,760 writes/day. Phase 2's lease renewals add
one write and one read per series per 30s; at eight series that is another
23,040 of each. Round up generously to ~1M writes and ~1M reads a month:

    1M write request units  @ $1.4256/M (eu-west-1)  = $1.43
    1M read  request units  @ $0.2851/M              = $0.29
    storage (tens of items)                          ~ $0.00
                                                     --------
                                                       ~$1.72/mo

That is not "pennies" — the phrase in the brief — but it is under two dollars,
against a t4g.micro line of $6.72 and an all-in EU box of ~$14/mo. Worth
writing down precisely rather than waving at, because the number that gets
waved at is the number nobody notices tripling.

**The account's own EC2 plan argued against DynamoDB** and specified S3
conditional writes for the wallet-owner lease instead
(`pmt-alpha/docs/ec2-euw-plan.md`: "S3 over DynamoDB: no table to manage, same
region, and the account has no DynamoDB footprint to grow"). That reasoning is
still sound and this is a genuine reversal, taken on the explicit instruction
to build on DynamoDB. `FleetStore` is an interface with two implementations
already, so an S3 `If-Match` backend is a swap and not a rewrite — see
DESIGN.md §"Why this store".

The throughput cap fails SAFE
-----------------------------
`OnDemandThroughput` bounds the bill by throttling, and throttling this table
means renewals start failing, which means holders fence themselves and stop
trading. A cost guard that stops the fleet sounds alarming until you notice the
alternative: an unbounded table where a retry loop bug bills real money for
hours. The guard is set ~12x above measured need, so reaching it is itself the
signal that something is looping.
"""

from __future__ import annotations

from typing import Any

from .store import REGION, TABLE_NAME

# ~12x headroom over the fleet's measured need (<0.4 writes/s, <1 read/s at
# eight series and two nodes). Ceiling if something pegs it 24/7: ~$22/mo.
MAX_WRITE_REQUEST_UNITS = 5
MAX_READ_REQUEST_UNITS = 5


def spec(table: str = TABLE_NAME) -> dict[str, Any]:
    return {
        "TableName": table,
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
        "OnDemandThroughput": {
            "MaxReadRequestUnits": MAX_READ_REQUEST_UNITS,
            "MaxWriteRequestUnits": MAX_WRITE_REQUEST_UNITS,
        },
        "DeletionProtectionEnabled": True,
        "Tags": [
            {"Key": "project", "Value": "pmt"},
            {"Key": "component", "Value": "fleet-orchestrator"},
        ],
    }


def create(table: str = TABLE_NAME, region: str = REGION, *, log=print) -> str:
    """Create the table if absent, then turn on TTL. Idempotent.

    TTL is enabled on `ttl` for heartbeats only, and it is garbage collection:
    DynamoDB deletes TTL'd items "typically within 48 hours", which is a sweep,
    not a clock. Nothing in the protocol reads it. Leases carry no TTL at all —
    a lease that vanished mid-flight would read as unheld and be instantly
    claimable, which is precisely the interval grace exists to prevent.
    """
    import boto3
    from botocore.exceptions import ClientError

    ddb = boto3.client("dynamodb", region_name=region)
    try:
        ddb.create_table(**spec(table))
        log(f"creating {table} in {region} ...")
        ddb.get_waiter("table_exists").wait(TableName=table)
        log("table active")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceInUseException":
            raise
        log(f"{table} already exists in {region}")

    try:
        ddb.update_time_to_live(
            TableName=table,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
        log("ttl enabled on attribute 'ttl' (heartbeat garbage collection only)")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # Re-enabling an already-enabled TTL is a validation error, not a
        # problem — this command has to stay safe to re-run.
        if code not in ("ValidationException",):
            raise
        log("ttl already enabled")
    return table
