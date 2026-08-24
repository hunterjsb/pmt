# The EU box's DynamoDB policy — AWAITING APPROVAL, NOT ATTACHED

`pmt-fleet-doctor-policy.json` in this directory is a proposed IAM policy for
the `pmt-eu-ssm` instance role. **Nothing has attached it.** It is checked in so
that granting a trading host its first non-SSM permission is a decision with a
diff behind it, the same way the units in this directory are.

## Why this is a real decision and not a formality

The README next to this file says, in bold, that the box has no S3 access
deliberately — the instance role is `AmazonSSMManagedInstanceCore` and nothing
else, and `ship-eu.sh` hands it a 15-minute presigned URL rather than standing
bucket credentials, because "handing a trading host standing bucket access to
buy one download is a worse trade than a URL that dies in 15 minutes."

This policy widens that role for the first time. The reasoning that kept S3 off
the box applies here too and the answer came out differently, so here is the
difference: S3 access bought one file download that a presigned URL already
covered. DynamoDB access buys something with no alternative — the box must
write its own heartbeat and, in phase 2, take and renew its own leases. A lease
the box cannot write is a lease it cannot hold, and a node that cannot hold a
lease cannot participate in failover at all.

## What it grants, and what it deliberately does not

| granted | scope |
| --- | --- |
| `PutItem`, `GetItem` | partition key `node#euw` only — **its own heartbeat row** |
| `PutItem`, `GetItem` | partition keys matching `series#*` — the leases |
| `GetItem` | partition key `fleet` — the kill switch, **read only** |

Everything is conditioned on `dynamodb:LeadingKeys`, so the grant is per-item,
not per-table. Three things follow that are worth having on purpose:

- **The box cannot forge the desktop's heartbeat.** `node#euw` is the only node
  row it can write. A compromised EU box cannot make the desktop look dark, and
  therefore cannot manufacture the appearance of a failover being due.
- **The box cannot turn the kill switch off.** It can read it — it must, to
  know whether it may claim — and it cannot write it. The freeze is Hunter's
  and stays Hunter's.
- **The box cannot enumerate the fleet.** No `Scan`, no `Query`. `Scan` is what
  the checker uses, and the checker runs on the desktop under operator
  credentials. `Query` is in neither path, so it is not granted; the brief
  allowed it and nothing uses it.

No `DeleteItem`, no `UpdateItem`, no `BatchWriteItem`, no table-level actions.
The lease protocol's only mutating primitive is a conditional `PutItem`, which
is what makes that list short.

## Attaching it, if you approve

```sh
aws iam create-policy \
  --policy-name pmt-fleet-doctor-euw \
  --policy-document file://deploy/eu/pmt-fleet-doctor-policy.json \
  --description "pmt-fleet heartbeat + series leases, scoped to the EU node's own keys"

aws iam attach-role-policy \
  --role-name pmt-eu-ssm \
  --policy-arn arn:aws:iam::350985642081:policy/pmt-fleet-doctor-euw
```

Verify the scope actually bit, rather than trusting the JSON:

```sh
# should be allowed
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::350985642081:role/pmt-eu-ssm \
  --action-names dynamodb:PutItem \
  --resource-arns arn:aws:dynamodb:eu-west-1:350985642081:table/pmt-fleet \
  --context-entries 'ContextKeyName=dynamodb:LeadingKeys,ContextKeyType=stringList,ContextKeyValues=node#euw'

# should be DENIED — the box must not be able to write the desktop's row
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::350985642081:role/pmt-eu-ssm \
  --action-names dynamodb:PutItem \
  --resource-arns arn:aws:dynamodb:eu-west-1:350985642081:table/pmt-fleet \
  --context-entries 'ContextKeyName=dynamodb:LeadingKeys,ContextKeyType=stringList,ContextKeyValues=node#desktop'
```

## The prerequisite this policy does not solve

**The EU box has no Python pmtrader checkout and no `uv`.** It has the
cross-compiled `pmengine` binary and nothing else — that is the whole point of
`ship-eu.sh`. So attaching this policy does not by itself get a heartbeat out of
the box. Two ways forward, and this is a separate decision from the IAM one:

- **(a) Beat by proxy from the desktop, via SSM.** The desktop already drives
  the box through `aws ssm send-command`; it can read `/status` and
  `arms-state.json` over SSM and write the `node#euw` row itself. Costs **no
  IAM change and no new software on the box**, and it is enough for all of
  phase 1. It does not extend to phase 2 — a lease must be written by the node
  that holds it, or it is not that node's lease — and it makes SSM
  reachability part of the observation path.
- **(b) Put pmtrader on the box.** Python 3.14 plus `uv` on a 1GB t4g.micro is
  feasible but is a new deployment surface on a host whose current software
  inventory is one static binary, and it is the same box where `rustc` OOMs.
  A single-file `boto3`-only beater is the middle road.

Recommendation: **(a) for phase 1**, and revisit (b) only when phase 2 is
actually being built, since that is the first point at which the box genuinely
must write for itself.
