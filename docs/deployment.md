# Deploying the chat app, and what the story actually turned out to be

Written 2026-08-26, from doing it rather than from reading about it. Issue #16
asks for exactly this and says why:

> The point is not the feature. The point is that it forces you through every
> platform's authentication and deployment story while the scope is still small
> enough to debug. […] write down anything that surprised you, because that is
> the deliverable that makes this issue worth doing early.

Nine things surprised me. They are in section 3. Sections 1 and 2 are the
procedure, so that the next deploy is not also an investigation.

---

## 1. The shape

```
  uv workspace ──▶ Dockerfile ──▶ ACR ──▶ Container App ──▶ default FQDN
   (--package)      linux/amd64   AcrPull    one replica     managed cert
                                  by identity
```

Nothing authenticates with a password anywhere in that line. The developer
pushes with their own Entra token (`az acr login`), the app pulls as
`id-chip-chat-app`, and the same identity is what reaches Foundry, Key Vault and
blob storage. There is no registry credential to rotate and none in the
Terraform.

The live URL is

<https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io>

on the Container Apps default domain with its automatic managed certificate.
Issue #4 was closed without buying a domain, so there is no DNS zone and no
certificate resource in the stack — which also means the hostname changes if the
app is ever recreated. Read it from `terraform output web_url`, never from here.

## 2. The procedure

```bash
make infra-apply                 # ACR, the AcrPull grant, the app's settings
make image                       # build for linux/amd64
make image-push                  # az acr login && docker push
make deploy                      # az containerapp update --image
make deploy-check                # poll until the NEW revision serves
```

`make deploy` is `az containerapp update`, not Terraform, and that is
deliberate: `compute.tf` has `ignore_changes = [template[0].container[0].image]`
so that the next `terraform apply` does not drag the app back to the quickstart
placeholder. Terraform owns the *estate*; a deploy owns the *image*.

## 3. What surprised me

### 3.1 `terraform plan` does not run at all without a Databricks login

The Databricks provider fails to configure without `databricks auth login`, and
a provider that fails to configure fails **the whole plan** — including the
resources that have nothing to do with Databricks:

```
Error: cannot read cluster policy: failed to validate workspace_id:
  default auth: cannot configure default credentials
```

So on a machine that is authenticated to Azure but not to Databricks, there is
no such thing as a partial plan. Everything in this issue was applied with
`-target`, which Terraform correctly complains is not for routine use. It is
worth knowing that "everything in Terraform" now carries a second
authentication prerequisite that has nothing to do with the resource you are
changing.

### 3.2 A subscription only has the resource providers somebody registered

Creating the registry failed with a 409:

```
MissingSubscriptionRegistration: The subscription is not registered to use
namespace 'Microsoft.ContainerRegistry'.
```

That reads like a permissions problem and is not one. The azurerm provider no
longer registers everything automatically; `az provider register --namespace
Microsoft.ContainerRegistry` fixes it in about a minute. `providers.tf` now
names it in `resource_providers_to_register` so the next subscription does not
rediscover this.

### 3.3 `provisioningState: Succeeded` does not mean "deployed"

This is the one that cost the most time. `az containerapp update` returns
`Succeeded`, the new revision reports `Healthy` and `Running`, its traffic weight
is 100 — and the URL still serves the **old** revision for another 30–60
seconds. When the old revision is Azure's quickstart placeholder, what you see is

```
$ curl https://…/healthz
404 page not found
$ curl https://…/
<title>Azure Container Apps</title>   ← the default service page
```

which looks exactly like an image that failed to start. It is not. The field
that actually moves is `latestReadyRevisionName`; until it equals
`latestRevisionName`, you are not deployed.

`make deploy-check` waits for exactly that, *and then* for the app's own
`/healthz`. Polling `/healthz` alone is not enough and it is worth saying why,
because it is the obvious thing to write: on the very first deploy the old
revision is Azure's placeholder and 404s, so `/healthz` happens to be a good
signal — and on every deploy after that the old revision serves `/healthz`
perfectly well, so it returns 200 immediately and tells you nothing.

### 3.4 The app never sees HTTPS, so `Secure` cookies need the forwarded scheme

Container Apps terminates TLS at ingress and forwards plain HTTP. A `Secure`
cookie decided from `request.url.scheme` would therefore never be set in
production — on a site that is entirely HTTPS. `X-Forwarded-Proto` is the answer,
and `_is_https` in `chip_chat.api.app` is the four lines that got this right on
purpose after getting it wrong by accident: the local test suite ran over
`http://testserver`, silently dropped the cookie, and every request minted a new
session. The bug looked like an order confirmation problem.

### 3.5 With one trusted proxy, the *last* `X-Forwarded-For` entry is the client

The proxy appends. Everything to the left of its entry is whatever the client
chose to claim. Taking `forwarded.split(",")[0]` — which is the form you see
most often — would let a caller re-roll its rate-limit bucket on every request by
sending a different header, which is precisely the opposite of what a
per-source rate limit is for.

### 3.6 Port 80 costs you root

An unprivileged container cannot bind below 1024 without a capability. The image
runs as uid 10001, so the app listens on 8000 and `var.web_target_port` is 8000.
Ingress is 443 either way; the visitor never sees the difference.

### 3.7 The demo's spend controls are one replica wide

The spend cap's counters are process-local (`api/README.md`). Two replicas are
two ledgers, so a daily ceiling of 2,000,000 tokens would mean 4,000,000 and the
per-session cap would apply to whichever replica happened to answer. That is why
`web_max_replicas` is **1** and the container runs **one** uvicorn worker — and
why raising either is a change to `BudgetLedger` first, not a scaling decision.

### 3.8 The kill switch works, and costs a revision

`CHIP_CHAT_KILL_SWITCH=on` as an application setting does exactly what
`api/README.md` promises: the entry page becomes the stop state, a turn returns
the stop-state message with HTTP 200, and no model is called. Measured end to
end on the deployed app.

What the runbook does not say is that changing an application setting creates a
new revision, and §3.3 applies — it took about 40 seconds from `Succeeded` to
the stop state actually being what the URL served. "A minute from a phone" holds,
but only just, and the old revision keeps answering for that window.

The runbook's second route, `touch /mnt/ops/stop`, is **not available today**:
no file share is mounted on the Container App. `FileKillSwitch` treats an
unreadable path as not thrown, so it costs nothing and blocks nothing — but the
honest statement is that there is one working kill switch, not three, and it is
the one that restarts the container.

### 3.9 The bug only the deployment could find

The first deployed run refused to place a confirmed order. Every unit test
passed. The model was calling `place_order`, the desk was correctly refusing it,
and the trace said `confirmation_state=rejected` — all working as designed.

What was missing was that **the model cannot see the Confirm button.** The press
arrives at the server, marks the draft, and the model is told nothing about it;
having been refused once, it goes on politely refusing forever. The fix is
`CONFIRMATION_NOTE` in `chip_chat.agent.loop` — a server-written note added to
the conversation only when the desk really did confirm something. It is a hint,
not the enforcement: a visitor who posts somebody else's draft id gets no note,
and `place_order` refuses them either way.

No test would have found this, because every test scripted the model's next move.
It took a real model, on the real URL, being genuinely unconvinced.

## 4. What it costs

| Thing | Charge |
| --- | --- |
| Container Registry, Basic | ~$5/month, standing — the only non-pay-per-use charge this adds |
| Container App | Per vCPU-second while a replica runs; `min_replicas = 0`, so idle is ~free |
| Model calls | Pay-per-token, GlobalStandard, no ceiling of their own — which is the spend cap's whole reason to exist |
| Log Analytics | Capped at 1 GB/day by `var.log_daily_quota_gb` |

The registry is the only new standing cost and it is a variable
(`var.container_registry_sku`) rather than a literal, because a standing charge
should be visible in a diff.

## 5. What is deployed but not shared

**The URL is live. It has not been given to anybody.** Issue #16 is explicit that
those are different things: *"Deploying is fine; publicising is not."*

The inline spend cap (`cc-fv1`) has landed **and is wired into the request
path** — see `api/README.md`, "Not callable — unconstructable-without". Every
turn passes a synchronous budget check before a model is reached, and there is
no route through the app that can skip it. Verified against the deployed URL by
throwing the kill switch and watching the stop state come back.

Two things are still true and worth saying plainly before anybody shares a link:

1. **The $150 Azure budget only alerts.** It prevents nothing, and `cc-05h`
   records that the test notification was never sent, so it has not been observed
   to fire end to end.
2. **Both model deployments are GlobalStandard**, which is pay-per-token with no
   ceiling of its own.

So the only thing bounding spend on that hostname is the code in `api/`. That is
what it was written for, and it is now genuinely in front of every request — but
it is worth knowing that it is the *only* thing there.

Before the link is shared, `#85` should trip the ceiling against the real
deployment. `SpendLimits.from_env` and `var.spend_caps` are how that is done
without a code change.
