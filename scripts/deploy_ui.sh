#!/bin/sh
set -eu

CONTEXT="${KUBE_CONTEXT:-kubernetes-admin@kubernetes}"
ZOO_NAMESPACE="${ZOO_NAMESPACE:-host1-tb-geonovum}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
UI_DIR="$PROJECT_DIR/ui"
MANIFEST="$PROJECT_DIR/k8s/routing-ui.yaml"

kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" create configmap zoo-routing-ui \
    --from-file=index.html="$UI_DIR/index.html" \
    --from-file=styles.css="$UI_DIR/styles.css" \
    --from-file=app.js="$UI_DIR/app.js" \
    --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -

kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" apply -f "$MANIFEST"
kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" rollout restart deployment/routing-ui
kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" rollout status \
    deployment/routing-ui --timeout=120s