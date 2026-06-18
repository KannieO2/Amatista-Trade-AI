#!/usr/bin/env bash
# Auto-create the Amatista TradeOS VM on Oracle Cloud, retrying until the
# Always-Free Ampere A1 capacity appears (Bogota / any tight region is chronically
# "out of capacity"). Run it in the OCI CLOUD SHELL — it is already authenticated
# as you, so there are NO API keys to set up.
#
# How to run:
#   1. Oracle Console → top-right → the ">_" Cloud Shell icon (wait for the prompt).
#   2. Upload this file (Cloud Shell ⋮ menu → Upload) OR paste it into nano.
#   3.   bash oracle-autocreate.sh
#   4. Leave it running. When capacity appears it creates the VM (4 OCPU / 24 GB),
#      prints the PUBLIC IP, and saves the SSH key. Then download the private key
#      (Cloud Shell ⋮ → Download → amatista-key) and tell me the IP.
#
# Knobs (override inline, e.g.  OCPUS=2 MEM=12 RETRY_SECS=20 bash oracle-autocreate.sh):
SHAPE="VM.Standard.A1.Flex"
OCPUS="${OCPUS:-4}"               # max Always-Free A1 = 4 OCPU
MEM="${MEM:-24}"                  # max Always-Free A1 = 24 GB
RETRY_SECS="${RETRY_SECS:-25}"    # wait between launch attempts
DISPLAY_NAME="${DISPLAY_NAME:-amatista-tradeos}"
set -uo pipefail

echo "==> Tenancy + availability domain"
TENANCY="${OCI_TENANCY:-}"
if [ -z "$TENANCY" ]; then
  read -rp "Pega tu Tenancy OCID (Perfil ▸ Tenancy, empieza con ocid1.tenancy...): " TENANCY
fi
COMPARTMENT="$TENANCY"            # create in the root compartment
AD=$(oci iam availability-domain list --compartment-id "$COMPARTMENT" \
     --query 'data[0].name' --raw-output)
echo "    AD = $AD"

echo "==> Imagen Ubuntu 22.04 (ARM) más reciente"
IMAGE=$(oci compute image list --compartment-id "$COMPARTMENT" \
        --operating-system "Canonical Ubuntu" --operating-system-version "22.04" \
        --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
        --query 'data[0].id' --raw-output)
echo "    image = $IMAGE"

echo "==> Llave SSH (genera una si no existe)"
if [ ! -f "$HOME/amatista-key.pub" ]; then
  ssh-keygen -t rsa -b 4096 -f "$HOME/amatista-key" -N "" -q
  echo "    creada ~/amatista-key (PRIVADA — bájala al final) + ~/amatista-key.pub"
fi
SSH_PUB="$HOME/amatista-key.pub"

echo "==> Red pública (idempotente, por nombre 'amatista-vcn')"
VCN=$(oci network vcn list --compartment-id "$COMPARTMENT" --display-name amatista-vcn \
      --query 'data[0].id' --raw-output 2>/dev/null || true)
if [ -z "${VCN:-}" ] || [ "$VCN" = "null" ]; then
  VCN=$(oci network vcn create --compartment-id "$COMPARTMENT" --display-name amatista-vcn \
        --cidr-blocks '["10.0.0.0/16"]' --wait-for-state AVAILABLE --query 'data.id' --raw-output)
  IG=$(oci network internet-gateway create --compartment-id "$COMPARTMENT" --vcn-id "$VCN" \
       --is-enabled true --display-name amatista-ig --wait-for-state AVAILABLE \
       --query 'data.id' --raw-output)
  RT=$(oci network vcn get --vcn-id "$VCN" --query 'data."default-route-table-id"' --raw-output)
  oci network route-table update --rt-id "$RT" --force \
    --route-rules "[{\"destination\":\"0.0.0.0/0\",\"networkEntityId\":\"$IG\"}]" >/dev/null
  SL=$(oci network vcn get --vcn-id "$VCN" --query 'data."default-security-list-id"' --raw-output)
  oci network security-list update --security-list-id "$SL" --force \
    --ingress-security-rules '[
      {"source":"0.0.0.0/0","protocol":"6","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},
      {"source":"0.0.0.0/0","protocol":"6","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":80,"max":80}}},
      {"source":"0.0.0.0/0","protocol":"6","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":443,"max":443}}}
    ]' \
    --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all","isStateless":false}]' >/dev/null
  SUBNET=$(oci network subnet create --compartment-id "$COMPARTMENT" --vcn-id "$VCN" \
           --display-name amatista-subnet --cidr-block 10.0.0.0/24 \
           --prohibit-public-ip-on-vnic false --wait-for-state AVAILABLE \
           --query 'data.id' --raw-output)
else
  SUBNET=$(oci network subnet list --compartment-id "$COMPARTMENT" --vcn-id "$VCN" \
           --display-name amatista-subnet --query 'data[0].id' --raw-output)
fi
echo "    subnet = $SUBNET (puertos 22/80/443 abiertos)"

echo "==> Reintentando launch ($SHAPE ${OCPUS}OCPU/${MEM}GB) cada ${RETRY_SECS}s hasta que haya cupo…"
n=0
while true; do
  n=$((n+1))
  OUT=$(oci compute instance launch \
    --compartment-id "$COMPARTMENT" \
    --availability-domain "$AD" \
    --display-name "$DISPLAY_NAME" \
    --shape "$SHAPE" \
    --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM}" \
    --image-id "$IMAGE" \
    --subnet-id "$SUBNET" \
    --assign-public-ip true \
    --ssh-authorized-keys-file "$SSH_PUB" \
    --wait-for-state RUNNING 2>&1)
  RC=$?
  if [ $RC -eq 0 ]; then
    INST=$(oci compute instance list --compartment-id "$COMPARTMENT" \
           --display-name "$DISPLAY_NAME" --lifecycle-state RUNNING \
           --query 'data[0].id' --raw-output)
    IP=$(oci compute instance list-vnics --instance-id "$INST" \
         --query 'data[0]."public-ip"' --raw-output)
    echo
    echo "=================================================================="
    echo "  ✅ VM CREADA (intento $n).  IP pública: $IP"
    echo "  Llave privada: ~/amatista-key  →  Cloud Shell ⋮ ▸ Download ▸ amatista-key"
    echo "  Pásame la IP y seguimos con el deploy."
    echo "=================================================================="
    break
  fi
  if echo "$OUT" | grep -qi 'capacity'; then
    echo "  [$n] sin cupo todavía — reintento en ${RETRY_SECS}s…"
  else
    echo "  [$n] error distinto (revisar):"; echo "$OUT" | tail -3
  fi
  sleep "$RETRY_SECS"
done
