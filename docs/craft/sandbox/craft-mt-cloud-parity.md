# Craft on MT prod cloud: infra parity checklist

Umbrella doc for everything needed to bring **Onyx Craft** up on the
multi-tenant prod cloud (`cloud.onyx.app`) to parity with the dev clusters
where Craft already runs. The PodTemplate work (the PR this doc ships with) is
one line item here; the rest is pre-existing infra that the dev clusters got
but MT prod did not.

## Context: two deployment surfaces

Craft today runs on **st-dev** and **craft-dev**, which are deployed via the
**Helm chart** using `customers/onyx/values.yaml` (st-dev) and
`customers/onyx/craft-dev-values.yaml` (craft-dev). Their craft infra was wired
by:

- **onyx#11787** — `feat(craft): codify Onyx Craft EKS infra (terraform + helm)`:
  the `craft_sandbox` Terraform module (S3 snapshot bucket + S3 IAM policy +
  `sandbox-file-sync` IRSA role), EKS OIDC output re-export, the optional
  sandbox node group (label/taint/IMDSv2/encrypted gp3/shared SG), and the Helm
  `sandbox-namespace.yaml` / `sandbox-rbac.yaml` / proxy templates.
- **cloud-deployment-yamls#328** — wires `craft.sandboxFileSyncRoleArn` into the
  st-dev + craft-dev Helm values, plus one-time SA adoption cleanup.
- **cloud-deployment-yamls#329** — instantiates the `craft_sandbox` TF module
  for the st-dev + craft-dev workspaces (bucket import runbook included).

**MT prod cloud (`cloud.onyx.app`) is a different surface.** ArgoCD
(`argocd-apps/onyx-prod.yaml`) syncs the **raw kustomize manifests under
`danswer/`** — it does **not** use the Helm chart. So none of the Helm-delivered
craft resources reach MT prod, and the `customers/onyx` TF wiring (#329) targets
the st-dev/craft-dev workspaces, not the prod/cloud cluster. Craft is currently
gated off there (`ENABLE_CRAFT` unset in `danswer/configmap/env-configmap.yaml`),
so nothing is broken yet — but every piece below must land before flipping it on.

## Parity matrix (MT prod = `danswer/` + the cloud TF workspace)

| Component | Source of truth (Helm/onyx) | MT prod status |
|---|---|---|
| `onyx-sandboxes` namespace | `sandbox-namespace.yaml` | No explicit manifest in `danswer/` (created out-of-band) — needs one |
| sandbox-manager RBAC (pods, **podtemplates**, services, **secrets**, pods/exec, **pods/log**) | `sandbox-rbac.yaml` | `danswer/role/api-server-role.yaml` has only pods, pods/exec, services — missing podtemplates, secrets, pods/log |
| `sandbox-file-sync` SA + IRSA annotation | `sandbox-rbac.yaml` + `craft.sandboxFileSyncRoleArn` | SA exists (`danswer/serviceaccount/sandbox-file-sync-sa.yaml`); verify IRSA + `skip-containers: sandbox` annotations |
| **sandbox-pod PodTemplate** | `sandbox-podtemplate.yaml` (this PR) | Missing — needs a static `danswer/` manifest (item 1) |
| Egress proxy (deployment/service/rbac/networkpolicy/pdb) | `templates/sandbox-proxy/*` | **Missing entirely** in `danswer/` |
| Proxy CA secret + CA bundle configmap | chart proxy templates | Missing — needed for the init container's TLS bundle |
| `SANDBOX_PROXY_HOST` / `SANDBOX_PROXY_PORT` | computed in `configmap.yaml` | Not set in `danswer/configmap/env-configmap.yaml` |
| Push signing key | `auth.sandboxPushSecret` | Present: `ONYX_SANDBOX_PUSH_PRIVATE_KEY` in `danswer/secret/onyx-secrets.yaml` |
| `SANDBOX_S3_BUCKET` / snapshot bucket + IRSA | `craft_sandbox` TF module | `SANDBOX_S3_BUCKET: onyx-cloud-craft-document-store` set; verify the TF module is instantiated for the cloud workspace (role/policy/bucket) |
| Sandbox node group (label/taint/IMDSv2/SG) | `craft_sandbox` TF (onyx#11787) | Verify the cloud cluster has Terraform-managed (or equivalent manual) sandbox nodes |
| Network firewall defense-in-depth | **Not codified anywhere** (called out in onyx#11787) | Open everywhere; decide if it gates MT launch |
| `ENABLE_CRAFT=true` | configMap | Off in MT prod (the launch switch — flip last) |

## Implementation strategy

Group the MT prod work by surface. Each item mirrors what the chart produces;
treat the Helm chart as the source of truth and render prod values into static
`danswer/` manifests.

### Raw manifests (`danswer/`)
1. **PodTemplate + RBAC reconcile.**
   - Add `danswer/podtemplate/sandbox-pod.yaml` — a static `v1/PodTemplate`
     named `sandbox-pod` in `onyx-sandboxes`, mirroring the Helm-rendered
     `templates/sandbox-podtemplate.yaml` with MT prod values baked in: image
     `onyxdotapp/sandbox:v0.1.53` (bump in lockstep with the chart default),
     ServiceAccount `sandbox-file-sync`, prod resource sizing (1000m/2Gi req,
     2000m/10Gi lim), Next.js port range 3010–3100, push-daemon port 8731, and
     the prod proxy host/port + CA configmap. Register it in
     `danswer/kustomization.yaml`.
   - Reconcile `danswer/role/api-server-role.yaml` to the Helm `sandbox-manager`
     Role's full verb set: it currently has only `pods`, `pods/exec`,
     `services` — add `podtemplates: get` (new: `read_namespaced_pod_template`),
     `secrets` (get/create/update/delete, for the per-pod opencode-auth Secret),
     and `pods/log: get`. Both `api-server-sa` and `celery-worker-sandbox-sa`
     bind to this one Role, so a single edit covers both.
   - The PodTemplate is the source-of-truth-bound copy of the chart template;
     add a lockstep-update comment in both so future pod-shape changes update
     both surfaces.
2. **Namespace** — add an `onyx-sandboxes` Namespace manifest (or confirm the
   out-of-band creation is acceptable for ArgoCD ownership).
3. **Egress proxy stack** — port `templates/sandbox-proxy/*` (deployment,
   service, rbac, networkpolicy, pdb) into `danswer/` with prod values, since the
   sandbox firewall init + all sandbox egress depends on it.
4. **Proxy CA secret + bundle configmap** — the init container reads the CA
   bundle; without it `firewall-init.sh` fails closed.
5. **ConfigMap** — set `SANDBOX_PROXY_HOST`/`SANDBOX_PROXY_PORT`,
   `SANDBOX_NAMESPACE`, `SANDBOX_SERVICE_ACCOUNT_NAME`, `SANDBOX_API_SERVER_URL`,
   and (last) `ENABLE_CRAFT=true`.

### Terraform (cloud workspace)
6. **`craft_sandbox` module** — instantiate it for the prod/cloud workspace
   (mirroring cloud-deployment-yamls#329 for st-dev/craft-dev): snapshot bucket
   `onyx-cloud-craft-document-store` (import if it already exists), S3 policy,
   `sandbox-file-sync` IRSA role trusted by
   `system:serviceaccount:onyx-sandboxes:sandbox-file-sync`. Feed the role ARN to
   the SA annotation.
7. **Sandbox node group** — enable/verify Terraform-managed sandbox nodes
   (`enable_craft_sandbox_node_group`) or confirm equivalent manual nodes carry
   the label/taint/IMDSv2/shared-SG invariant.
8. **Network firewall** — still uncodified (onyx#11787 left it out): regional
   network-firewall resources, firewall subnets, sandbox-subnet route-table
   updates, RFC1918/metadata denies, managed threat-intel rules. Decide whether
   this gates the MT launch or is fast-follow.

### Trust-boundary parity (verify, don't regress)
9. Confirm the pod-level `eks.amazonaws.com/skip-containers: sandbox` annotation
   reaches the rendered pod so the untrusted sandbox container never gets the
   `sandbox-file-sync` IRSA token (only the sidecar does). This is set by the
   manager on the pod, not the SA — verify it survives the PodTemplate path on
   MT prod.

## Tests / validation

- **Render parity:** diff the MT-prod-values Helm render of each craft template
  against its `danswer/` counterpart (PodTemplate, proxy, RBAC) — same shape
  modulo values.
- **RBAC:** `kubectl auth can-i {get podtemplates,create secrets,get pods/log,
  create pods} -n onyx-sandboxes --as=system:serviceaccount:danswer:api-server-sa`
  (and `celery-worker-sandbox-sa`) all return `yes`.
- **IRSA / trust boundary:** on a staging MT cluster, exec into a sandbox pod's
  `sandbox` container and confirm no AWS token is mounted; confirm the `sidecar`
  can write a snapshot to S3.
- **End-to-end smoke:** with `ENABLE_CRAFT=true` on a staging MT cluster,
  provision one sandbox per a test tenant — pod Ready, egress proxy reachable,
  preview loads, snapshot/restore round-trips, and a second tenant's sandbox is
  isolated (separate pod, separate opencode Secret, proxy injects the right PAT).
