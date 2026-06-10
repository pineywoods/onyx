# MT prod: ship the sandbox PodTemplate to the raw `danswer/` manifests

This plan lives in the onyx repo but describes changes to the **separate**
`cloud-deployment-yamls` repo. It is the follow-up to the Helm PodTemplate work
(PR that introduced `templates/sandbox-podtemplate.yaml`). It is one line item
in the broader MT parity checklist — see
[craft-mt-cloud-parity.md](./craft-mt-cloud-parity.md).

## Issues to Address

`_create_sandbox_pod` now reads a `sandbox-pod` PodTemplate from
`SANDBOX_NAMESPACE` (`onyx-sandboxes`) via `read_namespaced_pod_template` and
overlays the per-pod fields. The onyx Helm chart renders that PodTemplate and
grants the RBAC for it — which covers the **craft-dev** deployment
(`customers/onyx` via `helm upgrade`).

The **multi-tenant prod cloud** (`cloud.onyx.app`, `MULTI_TENANT: "true"`) is
deployed by ArgoCD from the raw kustomize manifests under `danswer/` — **not**
the Helm chart. Those manifests do not include the PodTemplate, and the sandbox
RBAC Role there is missing the verbs the new code path needs. So once
`ENABLE_CRAFT=true` is set in MT prod, every provision will fail:
- no `sandbox-pod` PodTemplate → `_create_sandbox_pod` raises the 404
  "PodTemplate not found" RuntimeError;
- even with the template present, the api-server (running in-cluster as its
  bound ServiceAccount) would 403 on `podtemplates: get`.

Goal: add the PodTemplate manifest and reconcile the sandbox RBAC Role in
`cloud-deployment-yamls/danswer/` so MT prod matches what the Helm chart
produces, **before** craft is enabled there.

## Important Notes

- **Not breaking today.** `danswer/configmap/env-configmap.yaml` does not set
  `ENABLE_CRAFT=true`, so craft is gated off in MT prod and `_create_sandbox_pod`
  never runs. This is pre-work to land before the craft launch flips that flag.
- **Two deployment surfaces, one shape.** The `danswer/` PodTemplate must stay
  byte-equivalent (modulo values) to the Helm-rendered
  `templates/sandbox-podtemplate.yaml`. Treat the Helm template as the source of
  truth and render MT prod values into a static manifest. Whenever the chart
  template changes, the `danswer/` copy must be updated in lockstep (call this
  out in both files with a cross-reference comment).
- **MT prod values** (from `danswer/configmap/env-configmap.yaml` /
  `customers/onyx`): image `onyxdotapp/sandbox:v0.1.53` (bump in lockstep with
  the chart default), namespace `onyx-sandboxes`, ServiceAccount
  `sandbox-file-sync`, prod resource sizing (1000m/2Gi req, 2000m/10Gi lim),
  Next.js port range 3010–3100, push-daemon port 8731, the proxy host/port and
  CA configmap as configured for prod.
- **RBAC is broader than just podtemplates.** `danswer/role/api-server-role.yaml`
  currently grants only `pods`, `pods/exec`, `services`. The Helm
  `sandbox-manager` Role (the authoritative required set) also grants
  `podtemplates: get` (new, from this work), `secrets`
  (get/create/update/delete — for the per-pod opencode-auth Secret), and
  `pods/log: get`. The MT prod Role must be reconciled to the full set, not just
  `podtemplates`, or enabling craft will fail on Secret creation too. Both
  `api-server-sa` and `celery-worker-sandbox-sa` bind to this one
  `api-server-role`, so a single Role edit covers both.
- The egress proxy + `sandbox-namespace` + proxy CA configmap also come from the
  Helm chart; verify their MT-prod equivalents exist in `danswer/` (or are
  applied another way) as part of the craft-launch checklist — out of scope for
  this plan but on the same critical path.

## Implementation strategy

In `cloud-deployment-yamls`:

1. **Add `danswer/podtemplate/sandbox-pod.yaml`** — a static `v1/PodTemplate`
   named `sandbox-pod` in `onyx-sandboxes`, mirroring the Helm-rendered template
   with MT prod values baked in. Header comment cross-referencing
   `deployment/helm/charts/onyx/templates/sandbox-podtemplate.yaml` in the onyx
   repo as the source of truth.
2. **Register it in `danswer/kustomization.yaml`** alongside the other sandbox
   resources.
3. **Reconcile `danswer/role/api-server-role.yaml`** to the Helm
   `sandbox-manager` Role's verb set: add `podtemplates: get`, `secrets`
   (get/create/update/delete), and `pods/log: get`.
4. Add a lockstep-update note in both the chart template and the `danswer/`
   manifest so future pod-shape changes update both.

In the onyx repo (already done in the originating PR; listed for the launch
checklist): chart renders the PodTemplate and the `sandbox-manager` Role grants
`podtemplates: get`.

## Tests

- **Render parity check:** diff the MT-prod-values `helm template --show-only
  templates/sandbox-podtemplate.yaml` against `danswer/podtemplate/sandbox-pod.yaml`
  to confirm the two surfaces produce the same pod shape (ignoring values that
  legitimately differ).
- **RBAC check on a cluster:** `kubectl auth can-i get podtemplates -n
  onyx-sandboxes --as=system:serviceaccount:danswer:api-server-sa` (and the
  celery SA) returns `yes`; same for `create secrets` and `get pods/log`.
- **Smoke:** in a staging MT cluster with `ENABLE_CRAFT=true`, apply the
  manifests and provision one sandbox end-to-end (pod reaches Ready, opencode
  Secret created, preview reachable).
