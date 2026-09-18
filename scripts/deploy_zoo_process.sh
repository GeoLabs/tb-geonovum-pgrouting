#!/bin/sh
set -eu

CONTEXT="${KUBE_CONTEXT:-kubernetes-admin@kubernetes}"
ZOO_NAMESPACE="${ZOO_NAMESPACE:-host1-tb-geonovum}"
ROUTING_NAMESPACE="${ROUTING_NAMESPACE:-routing}"
ZOO_KERNEL_DEPLOYMENT="${ZOO_KERNEL_DEPLOYMENT:-zoo-project-dru-zookernel}"
ZOO_FPM_DEPLOYMENT="${ZOO_FPM_DEPLOYMENT:-zoo-project-dru-zoofpm}"
SOURCE_SECRET="${SOURCE_SECRET:-pgrouting-db-secret}"
CLIENT_SECRET="${CLIENT_SECRET:-zoo-routing-db}"
PROCESS_CONFIGMAP="${PROCESS_CONFIGMAP:-zoo-routing-process}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROCESS_DIR=$(dirname "$SCRIPT_DIR")/zoo-process

secret_value() {
    kubectl --context "$CONTEXT" -n "$ROUTING_NAMESPACE" \
        get secret "$SOURCE_SECRET" -o "jsonpath={.data.$1}" | base64 -d
}

database=$(secret_value POSTGRES_DB)
username=$(secret_value POSTGRES_USER)
password=$(secret_value POSTGRES_PASSWORD)

kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" create secret generic "$CLIENT_SECRET" \
    --from-literal=ROUTING_PGHOST=pgrouting-db.routing.svc.cluster.local \
    --from-literal=ROUTING_PGPORT=5432 \
    --from-literal=ROUTING_PGDATABASE="$database" \
    --from-literal=ROUTING_PGUSER="$username" \
    --from-literal=ROUTING_PGPASSWORD="$password" \
    --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -

kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" create configmap "$PROCESS_CONFIGMAP" \
    --from-file=routing.zcfg="$PROCESS_DIR/routing.zcfg" \
    --from-file=routing_service.py="$PROCESS_DIR/routing_service.py" \
    --from-file=StageAHN.zcfg="$PROCESS_DIR/StageAHN.zcfg" \
    --from-file=ahn_stage_service.py="$PROCESS_DIR/ahn_stage_service.py" \
    --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -

kernel_pod=$(kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" get pod \
    -o name | grep "$ZOO_KERNEL_DEPLOYMENT" | head -n 1 | cut -d/ -f2)
kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" exec "$kernel_pod" -- \
    rm -f /opt/zooservices_user/anonymous/routing.zcfg \
        /opt/zooservices_user/anonymous/routing_service.py

for deployment_and_container in \
    "$ZOO_KERNEL_DEPLOYMENT:zookernel" \
    "$ZOO_FPM_DEPLOYMENT:zoofpm"
do
    deployment=${deployment_and_container%%:*}
    container=${deployment_and_container##*:}
    kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" patch deployment "$deployment" \
        --type=strategic \
        --patch "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$container\",\"volumeMounts\":[{\"name\":\"routing-process\",\"mountPath\":\"/usr/lib/cgi-bin/routing.zcfg\",\"subPath\":\"routing.zcfg\",\"readOnly\":true},{\"name\":\"routing-process\",\"mountPath\":\"/usr/lib/cgi-bin/routing_service.py\",\"subPath\":\"routing_service.py\",\"readOnly\":true},{\"name\":\"routing-process\",\"mountPath\":\"/usr/lib/cgi-bin/StageAHN.zcfg\",\"subPath\":\"StageAHN.zcfg\",\"readOnly\":true},{\"name\":\"routing-process\",\"mountPath\":\"/usr/lib/cgi-bin/ahn_stage_service.py\",\"subPath\":\"ahn_stage_service.py\",\"readOnly\":true},{\"name\":\"routing-db-secret\",\"mountPath\":\"/etc/zoo-routing\",\"readOnly\":true}]}],\"volumes\":[{\"name\":\"routing-process\",\"configMap\":{\"name\":\"$PROCESS_CONFIGMAP\"}},{\"name\":\"routing-db-secret\",\"secret\":{\"secretName\":\"$CLIENT_SECRET\"}}]}}}}"
done

kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" set env \
    deployment/"$ZOO_KERNEL_DEPLOYMENT" --from=secret/"$CLIENT_SECRET"
kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" set env \
    deployment/"$ZOO_FPM_DEPLOYMENT" --from=secret/"$CLIENT_SECRET"

kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" rollout restart \
    deployment/"$ZOO_KERNEL_DEPLOYMENT" deployment/"$ZOO_FPM_DEPLOYMENT"

kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" rollout status \
    deployment/"$ZOO_KERNEL_DEPLOYMENT" --timeout=300s
kubectl --context "$CONTEXT" -n "$ZOO_NAMESPACE" rollout status \
    deployment/"$ZOO_FPM_DEPLOYMENT" --timeout=300s