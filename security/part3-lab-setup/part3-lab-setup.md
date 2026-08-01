## PRE-REQUISITES
Before starting building this lab environment, we need the following components installed on the host machine:
- docker: https://docs.docker.com/engine/install/
- kubectl: https://kubernetes.io/docs/tasks/tools/
- k3d: https://k3d.io/stable/ (on MacOS, you can install it via Homebrew: `brew install k3d`)
- helm: https://helm.sh/docs/intro/install/
- CloudNativePG Plugin: https://cloudnative-pg.io/documentation/1.18/cnpg-plugin/

In the following, we'll use the placeholder <HOST_IP> to represent the IP of the host running docker.

## STEP 1: Create the servers

### 1. Allow Docker Container-to-Host Traffic
```bash
sudo iptables -P FORWARD ACCEPT
sudo iptables -I INPUT -p tcp --dport 8200 -j ACCEPT
```

### 2. The HashiCorp vault

Run Vault as a container directly on your host network so both k3d clusters can reach it at your Docker host's LAN IP (e.g., <HOST_IP>:8200):
```bash
docker run -d \
  --name external-vault \
  --restart unless-stopped \
  --net host \
  -e 'VAULT_DEV_ROOT_TOKEN_ID=root' \
  -e 'VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200' \
  hashicorp/vault:latest
```

### 3. The two K8s clusters

```bash
# Cluster A (Primary Cluster)
k3d cluster create cluster-a \
  --agents 1 \
  --k3s-arg "--disable=traefik@server:*"

# Cluster B (Standby Cluster)
k3d cluster create cluster-b \
  --agents 1 \
  --k3s-arg "--disable=traefik@server:*"
```

Note: Disabling traefik saves ~150MB of RAM per cluster since we don't need an HTTP Ingress controller for database replication


## STEP 3: Install Core Operators on Both Servers

### 1- Install cert-manager & RBAC

```bash
# Cluster A
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.1/cert-manager.yaml --context k3d-cluster-a

# Cluster B
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.1/cert-manager.yaml --context k3d-cluster-b

# Output example x2:
namespace/cert-manager created
customresourcedefinition.apiextensions.k8s.io/certificaterequests.cert-manager.io created
customresourcedefinition.apiextensions.k8s.io/certificates.cert-manager.io created
customresourcedefinition.apiextensions.k8s.io/challenges.acme.cert-manager.io created
customresourcedefinition.apiextensions.k8s.io/clusterissuers.cert-manager.io created
customresourcedefinition.apiextensions.k8s.io/issuers.cert-manager.io created
customresourcedefinition.apiextensions.k8s.io/orders.acme.cert-manager.io created
serviceaccount/cert-manager-cainjector created
serviceaccount/cert-manager created
serviceaccount/cert-manager-webhook created
clusterrole.rbac.authorization.k8s.io/cert-manager-cainjector created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-issuers created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-clusterissuers created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-certificates created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-orders created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-challenges created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-ingress-shim created
clusterrole.rbac.authorization.k8s.io/cert-manager-cluster-view created
clusterrole.rbac.authorization.k8s.io/cert-manager-view created
clusterrole.rbac.authorization.k8s.io/cert-manager-edit created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-approve:cert-manager-io created
clusterrole.rbac.authorization.k8s.io/cert-manager-controller-certificatesigningrequests created
clusterrole.rbac.authorization.k8s.io/cert-manager-webhook:subjectaccessreviews created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-cainjector created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-issuers created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-clusterissuers created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-certificates created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-orders created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-challenges created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-ingress-shim created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-approve:cert-manager-io created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-controller-certificatesigningrequests created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-webhook:subjectaccessreviews created
role.rbac.authorization.k8s.io/cert-manager-cainjector:leaderelection created
role.rbac.authorization.k8s.io/cert-manager:leaderelection created
role.rbac.authorization.k8s.io/cert-manager-webhook:dynamic-serving created
rolebinding.rbac.authorization.k8s.io/cert-manager-cainjector:leaderelection created
rolebinding.rbac.authorization.k8s.io/cert-manager:leaderelection created
rolebinding.rbac.authorization.k8s.io/cert-manager-webhook:dynamic-serving created
service/cert-manager created
service/cert-manager-webhook created
deployment.apps/cert-manager-cainjector created
deployment.apps/cert-manager created
deployment.apps/cert-manager-webhook created
mutatingwebhookconfiguration.admissionregistration.k8s.io/cert-manager-webhook created
validatingwebhookconfiguration.admissionregistration.k8s.io/cert-manager-webhook created

kubectl apply --context k3d-cluster-a -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: vault-auth-delegator
subjects:
  - kind: ServiceAccount
    name: external-secrets
    namespace: external-secrets
  - kind: ServiceAccount
    name: cert-manager
    namespace: cert-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cert-manager-token-request
rules:
  - apiGroups: [""]
    resources: ["serviceaccounts/token"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cert-manager-token-request
subjects:
  - kind: ServiceAccount
    name: cert-manager
    namespace: cert-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cert-manager-token-request
EOF

kubectl apply --context k3d-cluster-b -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: vault-auth-delegator
subjects:
  - kind: ServiceAccount
    name: external-secrets
    namespace: external-secrets
  - kind: ServiceAccount
    name: cert-manager
    namespace: cert-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cert-manager-token-request
rules:
  - apiGroups: [""]
    resources: ["serviceaccounts/token"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cert-manager-token-request
subjects:
  - kind: ServiceAccount
    name: cert-manager
    namespace: cert-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cert-manager-token-request
EOF

# Output example x2:
clusterrolebinding.rbac.authorization.k8s.io/vault-auth-delegator created
clusterrole.rbac.authorization.k8s.io/cert-manager-token-request created
clusterrolebinding.rbac.authorization.k8s.io/cert-manager-token-request created
```

### 2- Install external-secrets

```bash
# Cluster A
kubectl apply -f https://raw.githubusercontent.com/external-secrets/external-secrets/main/deploy/crds/bundle.yaml --server-side --context k3d-cluster-a

# Cluster B
kubectl apply -f https://raw.githubusercontent.com/external-secrets/external-secrets/main/deploy/crds/bundle.yaml --server-side --context k3d-cluster-b

# Output example x2:
customresourcedefinition.apiextensions.k8s.io/clusterexternalsecrets.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/clusterpushsecrets.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/clustersecretstores.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/externalsecrets.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/pushsecrets.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/secretstores.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/acraccesstokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/beyondtrustworkloadcredentialsdynamicsecrets.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/cloudsmithaccesstokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/clustergenerators.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/ecrauthorizationtokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/fakes.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/gcraccesstokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/generatorstates.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/githubaccesstokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/gitlabdeploytokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/grafanas.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/mfas.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/passwords.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/quayaccesstokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/sshkeys.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/stssessiontokens.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/uuids.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/vaultdynamicsecrets.generators.external-secrets.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/webhooks.generators.external-secrets.io serverside-applied
```

```bash
# 1. Add and update the Helm repository
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

# Output example:
"external-secrets" has been added to your repositories
Hang tight while we grab the latest from your chart repositories...
...Successfully got an update from the "external-secrets" chart repository
Update Complete. ⎈Happy Helming!⎈

# 2. Deploy the ESO operator on Cluster A (skipping CRD reinstall)
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --set installCRDs=false \
  --kube-context k3d-cluster-a

# 3. Deploy the ESO operator on Cluster B (skipping CRD reinstall)
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --set installCRDs=false \
  --kube-context k3d-cluster-b

# Output example x2:
NAME: external-secrets
LAST DEPLOYED: Thu Jul 30 21:37:36 2026
NAMESPACE: external-secrets
STATUS: deployed
REVISION: 1
DESCRIPTION: Install complete
TEST SUITE: None
NOTES:
external-secrets has been deployed successfully in namespace external-secrets!

In order to begin using ExternalSecrets, you will need to set up a SecretStore
or ClusterSecretStore resource (for example, by creating a 'vault' SecretStore).

More information on the different types of SecretStores and how to configure them
can be found in our Github: https://github.com/external-secrets/external-secrets

# 4. After a couple of minutes, check pods creation
kubectl get pods -n external-secrets --context k3d-cluster-a

# Output example:
NAME                                                READY   STATUS    RESTARTS   AGE
external-secrets-6d6bf58975-dbhj5                   1/1     Running   0          92s
external-secrets-cert-controller-66f755d66d-dlbwz   1/1     Running   0          92s
external-secrets-webhook-7bc6d89c95-cvtqr           1/1     Running   0          92s

kubectl get pods -n external-secrets --context k3d-cluster-b

#Output example:
NAME                                                READY   STATUS    RESTARTS   AGE
external-secrets-6d6bf58975-p96vh                   1/1     Running   0          39s
external-secrets-cert-controller-66f755d66d-slfgt   1/1     Running   0          39s
external-secrets-webhook-7bc6d89c95-pb64m           1/1     Running   0          39s
```

### 3- Install cloudnative-pg

```bash
# Cluster A
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-1.30.0.yaml --context k3d-cluster-a

# Cluster B
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-1.30.0.yaml --context k3d-cluster-b

# Output example x2:
namespace/cnpg-system serverside-applied
customresourcedefinition.apiextensions.k8s.io/backups.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/clusterimagecatalogs.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/clusters.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/databaseroles.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/databases.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/failoverquorums.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/imagecatalogs.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/poolers.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/publications.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/scheduledbackups.postgresql.cnpg.io serverside-applied
customresourcedefinition.apiextensions.k8s.io/subscriptions.postgresql.cnpg.io serverside-applied
serviceaccount/cnpg-manager serverside-applied
clusterrole.rbac.authorization.k8s.io/cnpg-database-editor-role serverside-applied
clusterrole.rbac.authorization.k8s.io/cnpg-database-viewer-role serverside-applied
clusterrole.rbac.authorization.k8s.io/cnpg-manager serverside-applied
clusterrole.rbac.authorization.k8s.io/cnpg-publication-editor-role serverside-applied
clusterrole.rbac.authorization.k8s.io/cnpg-publication-viewer-role serverside-applied
clusterrole.rbac.authorization.k8s.io/cnpg-subscription-editor-role serverside-applied
clusterrole.rbac.authorization.k8s.io/cnpg-subscription-viewer-role serverside-applied
clusterrolebinding.rbac.authorization.k8s.io/cnpg-manager-rolebinding serverside-applied
configmap/cnpg-default-monitoring serverside-applied
service/cnpg-webhook-service serverside-applied
deployment.apps/cnpg-controller-manager serverside-applied
mutatingwebhookconfiguration.admissionregistration.k8s.io/cnpg-mutating-webhook-configuration serverside-applied
validatingwebhookconfiguration.admissionregistration.k8s.io/cnpg-validating-webhook-configuration serverside-applied
```

### 4- Install and Configure MetalLB for Cross-Cluster Routing

Installation:

```bash
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.16.1/config/manifests/metallb-native.yaml --context k3d-cluster-a

kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.16.1/config/manifests/metallb-native.yaml --context k3d-cluster-b

# Output example x2:
namespace/metallb-system created
customresourcedefinition.apiextensions.k8s.io/bfdprofiles.metallb.io created
customresourcedefinition.apiextensions.k8s.io/bgpadvertisements.metallb.io created
customresourcedefinition.apiextensions.k8s.io/bgppeers.metallb.io created
customresourcedefinition.apiextensions.k8s.io/communities.metallb.io created
customresourcedefinition.apiextensions.k8s.io/configurationstates.metallb.io created
customresourcedefinition.apiextensions.k8s.io/ipaddresspools.metallb.io created
customresourcedefinition.apiextensions.k8s.io/l2advertisements.metallb.io created
customresourcedefinition.apiextensions.k8s.io/servicebgpstatuses.metallb.io created
customresourcedefinition.apiextensions.k8s.io/servicel2statuses.metallb.io created
serviceaccount/controller created
serviceaccount/speaker created
role.rbac.authorization.k8s.io/controller created
role.rbac.authorization.k8s.io/pod-lister created
clusterrole.rbac.authorization.k8s.io/metallb-system:controller created
clusterrole.rbac.authorization.k8s.io/metallb-system:speaker created
rolebinding.rbac.authorization.k8s.io/controller created
rolebinding.rbac.authorization.k8s.io/pod-lister created
clusterrolebinding.rbac.authorization.k8s.io/metallb-system:controller created
clusterrolebinding.rbac.authorization.k8s.io/metallb-system:speaker created
configmap/metallb-excludel2 created
secret/metallb-webhook-cert created
service/metallb-webhook-service created
deployment.apps/controller created
daemonset.apps/speaker created
validatingwebhookconfiguration.admissionregistration.k8s.io/metallb-webhook-configuration created
```

Check your Docker Subnets for MetalLB:

```bash
docker network inspect k3d-cluster-a -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'

# Output example:
172.18.0.0/16

docker network inspect k3d-cluster-b -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'

# Output example:
172.19.0.0/16
```

Configure MetalLB on both Clusters using a slice of their respective subnet:

```bash
# Cluster A
kubectl apply --context k3d-cluster-a -f - <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: first-pool
  namespace: metallb-system
spec:
  addresses:
  - 172.18.250.10-172.18.250.100
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: empty
  namespace: metallb-system
EOF

# Cluster B
kubectl apply --context k3d-cluster-b -f - <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: first-pool
  namespace: metallb-system
spec:
  addresses:
  - 172.19.250.10-172.19.250.100
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: empty
  namespace: metallb-system
EOF

# Output example x2:
ipaddresspool.metallb.io/first-pool created
l2advertisement.metallb.io/empty created
```

## STEP 5: Multi-cluster Vault Authentication

Since Vault is running outside the clusters on `<HOST_IP>`, it needs a way to authenticate ServiceAccount tokens from both `cluster-a` and `cluster-b`.

To prevent cross-cluster token collisions, we enable two separate Kubernetes Auth engine paths in Vault:

- `auth/k8s-cluster-a` → Validates tokens against Cluster A's K8s API Server.

- `auth/k8s-cluster-b` → Validates tokens against Cluster B's K8s API Server.

### 1- Find K8s Ports & Extract CA Certificates

```bash
PORT_A=$(kubectl config view --context k3d-cluster-a --minify -o jsonpath='{.clusters[0].cluster.server}' | cut -d: -f3)
PORT_B=$(kubectl config view --context k3d-cluster-b --minify -o jsonpath='{.clusters[0].cluster.server}' | cut -d: -f3)

kubectl config view --raw --context k3d-cluster-a --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > ca-cluster-a.crt
kubectl config view --raw --context k3d-cluster-b --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > ca-cluster-b.crt

docker cp ca-cluster-a.crt external-vault:/tmp/ca-cluster-a.crt
docker cp ca-cluster-b.crt external-vault:/tmp/ca-cluster-b.crt

# Ouput example:
Successfully copied 570B (transferred 2.56kB) to external-vault:/tmp/ca-cluster-a.crt
Successfully copied 570B (transferred 2.56kB) to external-vault:/tmp/ca-cluster-b.crt
```

### 2- Generate Long-Lived Token Reviewer JWTs

```bash
TOKEN_A=$(kubectl create token cert-manager -n cert-manager --duration=87600h --context k3d-cluster-a)
TOKEN_B=$(kubectl create token cert-manager -n cert-manager --duration=87600h --context k3d-cluster-b)
```

### 4- Enable the Auths Paths
```bash
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault auth enable -path=k8s-cluster-a kubernetes

docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault auth enable -path=k8s-cluster-b kubernetes

# Output example:
Success! Enabled kubernetes auth method at: k8s-cluster-a/
Success! Enabled kubernetes auth method at: k8s-cluster-b/
```

### 5- Configure the cluster Endpoints
```bash
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write auth/k8s-cluster-a/config \
    kubernetes_host="https://127.0.0.1:${PORT_A}" \
    kubernetes_ca_cert=@/tmp/ca-cluster-a.crt \
    token_reviewer_jwt="$TOKEN_A" \
    disable_iss_validation=true

docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write auth/k8s-cluster-b/config \
    kubernetes_host="https://127.0.0.1:${PORT_B}" \
    kubernetes_ca_cert=@/tmp/ca-cluster-b.crt \
    token_reviewer_jwt="$TOKEN_B" \
    disable_iss_validation=true

# Output example:
Success! Data written to: auth/k8s-cluster-a/config
Success! Data written to: auth/k8s-cluster-b/config
```

### 5- Create the Roles for Cluster A & B
```bash
# Cluster A Roles
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write auth/k8s-cluster-a/role/eso \
    bound_service_account_names=external-secrets \
    bound_service_account_namespaces=external-secrets \
    policies=eso-policy \
    ttl=1h

docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write auth/k8s-cluster-a/role/cert-manager \
    bound_service_account_names=cert-manager \
    bound_service_account_namespaces=cert-manager \
    audience="vault" \
    policies=cert-manager-policy \
    ttl=1h

# Cluster B Roles
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write auth/k8s-cluster-b/role/eso \
    bound_service_account_names=external-secrets \
    bound_service_account_namespaces=external-secrets \
    policies=eso-policy \
    ttl=1h

docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write auth/k8s-cluster-b/role/cert-manager \
    bound_service_account_names=cert-manager \
    bound_service_account_namespaces=cert-manager \
    audience="vault" \
    policies=cert-manager-policy \
    ttl=1h

# Ouput example x2 (the warn ings are expected):
WARNING! The following warnings were returned from Vault:

  * Role eso does not have an audience configured. While audiences are not
  required, consider specifying one if your use case would benefit from
  additional JWT claim verification.

Success! Data written to: auth/k8s-cluster-a/role/cert-manager
```

### 6- Enable Secrets Engines
```bash
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault secrets enable -path=kv kv-v2

docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault secrets enable pki

# Output example:
Success! Enabled the kv-v2 secrets engine at: kv/
Success! Enabled the pki secrets engine at: pki/
```

### 7- Generate Root CA for PKI (Valid for 10 years)
```bash
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write pki/root/generate/internal common_name="cnpg-root-ca" ttl=87600h

# Output example:
WARNING! The following warnings were returned from Vault:

  * This mount hasn't configured any authority information access (AIA)
  fields; this may make it harder for systems to find missing certificates
  in the chain or to validate revocation status of certificates. Consider
  updating /config/urls or the newly generated issuer with this information.

Key              Value
---              -----
certificate      -----BEGIN CERTIFICATE-----
MIIDODCCAiCgAwIBAgIUOXDsOcqdwl9dDnpXmBfjwkbNxCswDQYJKoZIhvcNAQEL
BQAwFzEVMBMGA1UEAxMMY25wZy1yb290LWNhMB4XDTI2MDczMDE3NTkzNVoXDTI2
MDgzMTE4MDAwNVowFzEVMBMGA1UEAxMMY25wZy1yb290LWNhMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzwWTyFz8cuNSW9gYv132eFEWCgmJIz4BCahl
BmnYrM1MVHUxHVUIICdb5QmVYWWnOzE6eRv97PWaikLWDALaGxrWjRUHu9GwhkOU
ipYdRwHO4ungyw/0UF4g40qCWn+OkgzNSdqJ6JU8xw5lX3s+z8Cgnu40ZWTCD+l+
0rH0ZenlnjtLSFg/6+xIMBWvxKKSn4fObMLd7iiujKI86T0B015n+R1MRfMLmNUl
hBIUTIDpRXyiUq6tvb4z3gnbmOA91GN/dUfEoZceVwMp5WDCtA/7VbTE/oMok8a3
EDDDKz4/Fa5xQYshdw8uP0jYLu3JttwtUaCjg/YXePx66DTw1wIDAQABo3wwejAO
BgNVHQ8BAf8EBAMCAQYwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUXYEVZX3F
IMuGXvAQBcf5o7hvH1IwHwYDVR0jBBgwFoAUXYEVZX3FIMuGXvAQBcf5o7hvH1Iw
FwYDVR0RBBAwDoIMY25wZy1yb290LWNhMA0GCSqGSIb3DQEBCwUAA4IBAQCQy6s2
sXoEwgaJKjxj982m02rQ3tLhL/m7+g9w3sifEuGjcVKnR/TUa9CZqUW09kzfEDml
VBZMfrb1xwF4M4ANHsj6zPgAX2HBYhy6+YuCPfRNQ18FtoV53kgR2tYedH6+hWNz
ufpMu/VMRjvB5IeNmdNYY2KXXPHiP9X3GuJXauFw7fFmEYs/CbzgMe+jEtTrz1ka
cdFWLS9YkrzX7VmWdCKN9/20EhY07jD8hEfmgv82RtlP6Fej5C8/az7gz1Xp0mMI
jHiMFHhcVUnjllMR7Q1pTe4sT12p+h9tTnskytie1rqgDYkuMAgT2vdVMDC4vgM8
xKyKwKBg905FjYsR
-----END CERTIFICATE-----
expiration       1788199205
issuer_id        99c2b30b-414d-6901-ccbf-205a430fd408
issuer_name      n/a
issuing_ca       -----BEGIN CERTIFICATE-----
MIIDODCCAiCgAwIBAgIUOXDsOcqdwl9dDnpXmBfjwkbNxCswDQYJKoZIhvcNAQEL
BQAwFzEVMBMGA1UEAxMMY25wZy1yb290LWNhMB4XDTI2MDczMDE3NTkzNVoXDTI2
MDgzMTE4MDAwNVowFzEVMBMGA1UEAxMMY25wZy1yb290LWNhMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzwWTyFz8cuNSW9gYv132eFEWCgmJIz4BCahl
BmnYrM1MVHUxHVUIICdb5QmVYWWnOzE6eRv97PWaikLWDALaGxrWjRUHu9GwhkOU
ipYdRwHO4ungyw/0UF4g40qCWn+OkgzNSdqJ6JU8xw5lX3s+z8Cgnu40ZWTCD+l+
0rH0ZenlnjtLSFg/6+xIMBWvxKKSn4fObMLd7iiujKI86T0B015n+R1MRfMLmNUl
hBIUTIDpRXyiUq6tvb4z3gnbmOA91GN/dUfEoZceVwMp5WDCtA/7VbTE/oMok8a3
EDDDKz4/Fa5xQYshdw8uP0jYLu3JttwtUaCjg/YXePx66DTw1wIDAQABo3wwejAO
BgNVHQ8BAf8EBAMCAQYwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUXYEVZX3F
IMuGXvAQBcf5o7hvH1IwHwYDVR0jBBgwFoAUXYEVZX3FIMuGXvAQBcf5o7hvH1Iw
FwYDVR0RBBAwDoIMY25wZy1yb290LWNhMA0GCSqGSIb3DQEBCwUAA4IBAQCQy6s2
sXoEwgaJKjxj982m02rQ3tLhL/m7+g9w3sifEuGjcVKnR/TUa9CZqUW09kzfEDml
VBZMfrb1xwF4M4ANHsj6zPgAX2HBYhy6+YuCPfRNQ18FtoV53kgR2tYedH6+hWNz
ufpMu/VMRjvB5IeNmdNYY2KXXPHiP9X3GuJXauFw7fFmEYs/CbzgMe+jEtTrz1ka
cdFWLS9YkrzX7VmWdCKN9/20EhY07jD8hEfmgv82RtlP6Fej5C8/az7gz1Xp0mMI
jHiMFHhcVUnjllMR7Q1pTe4sT12p+h9tTnskytie1rqgDYkuMAgT2vdVMDC4vgM8
xKyKwKBg905FjYsR
-----END CERTIFICATE-----
key_id           882d5ea3-69c5-80d2-64c0-c54f818c38e5
key_name         n/a
serial_number    39:70:ec:39:ca:9d:c2:5f:5d:0e:7a:57:98:17:e3:c2:46:cd:c4:2b
```

### 8- Create PKI Roles (Server DNS vs Client mTLS)
```bash
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write pki/roles/cnpg-server-role \
    allowed_domains="svc,svc.cluster.local" \
    allow_subdomains=true \
    allow_any_name=false \
    enforce_hostnames=true \
    require_cn=false \
    max_ttl=720h

# Output example:
Key                                   Value
---                                   -----
allow_any_name                        false
allow_bare_domains                    false
allow_glob_domains                    false
allow_ip_sans                         true
allow_localhost                       true
allow_subdomains                      true
allow_token_displayname               false
allow_wildcard_certificates           true
allowed_domains                       [svc svc.cluster.local]
allowed_domains_template              false
allowed_other_sans                    []
allowed_serial_numbers                []
allowed_uri_sans                      []
allowed_uri_sans_template             false
allowed_user_ids                      []
basic_constraints_valid_for_non_ca    false
client_flag                           true
cn_validations                        [email hostname]
code_signing_flag                     false
country                               []
email_protection_flag                 false
enforce_hostnames                     true
ext_key_usage                         []
ext_key_usage_oids                    []
generate_lease                        false
issuer_ref                            default
key_bits                              2048
key_type                              rsa
key_usage                             [DigitalSignature KeyAgreement KeyEncipherment]
locality                              []
max_ttl                               720h
no_store                              false
not_after                             n/a
not_before_duration                   30s
organization                          []
ou                                    []
policy_identifiers                    []
postal_code                           []
province                              []
require_cn                            false
serial_number_source                  json-csr
server_flag                           true
signature_bits                        256
street_address                        []
ttl                                   0s
use_csr_common_name                   true
use_csr_sans                          true
use_pss                               false

docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault write pki/roles/cnpg-client-role \
    allow_any_name=true \
    enforce_hostnames=false \
    require_cn=true \
    max_ttl=720h

# Output example:
Key                                   Value
---                                   -----
allow_any_name                        true
allow_bare_domains                    false
allow_glob_domains                    false
allow_ip_sans                         true
allow_localhost                       true
allow_subdomains                      false
allow_token_displayname               false
allow_wildcard_certificates           true
allowed_domains                       []
allowed_domains_template              false
allowed_other_sans                    []
allowed_serial_numbers                []
allowed_uri_sans                      []
allowed_uri_sans_template             false
allowed_user_ids                      []
basic_constraints_valid_for_non_ca    false
client_flag                           true
cn_validations                        [email hostname]
code_signing_flag                     false
country                               []
email_protection_flag                 false
enforce_hostnames                     false
ext_key_usage                         []
ext_key_usage_oids                    []
generate_lease                        false
issuer_ref                            default
key_bits                              2048
key_type                              rsa
key_usage                             [DigitalSignature KeyAgreement KeyEncipherment]
locality                              []
max_ttl                               720h
no_store                              false
not_after                             n/a
not_before_duration                   30s
organization                          []
ou                                    []
policy_identifiers                    []
postal_code                           []
province                              []
require_cn                            true
serial_number_source                  json-csr
server_flag                           true
signature_bits                        256
street_address                        []
ttl                                   0s
use_csr_common_name                   true
use_csr_sans                          true
use_pss                               false
```

### 9. Create Policies
```bash
docker exec -i -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault policy write eso-policy - <<EOF
path "kv/data/cnpg/*" {
  capabilities = ["read"]
}
EOF

docker exec -i -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault policy write cert-manager-policy - <<EOF
path "pki/sign/cnpg-*" {
  capabilities = ["create", "update"]
}
EOF

# Output example:
Success! Uploaded policy: eso-policy
Success! Uploaded policy: cert-manager-policy
```


## STEP 4: Deploy Vault Backends & Issuers

IMPORTANT NOTE: be careful to replace <HOST_IP>
```bash
kubectl apply --context k3d-cluster-a -f - <<EOF
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "http://<HOST_IP>:8200"
      path: "kv"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "k8s-cluster-a"
          role: "eso"
          serviceAccountRef:
            name: "external-secrets"
            namespace: "external-secrets"
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-server-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-server-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-a"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences:
            - "vault"
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-client-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-client-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-a"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences:
            - "vault"
EOF

kubectl apply --context k3d-cluster-b -f - <<EOF
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "http://<HOST_IP>:8200"
      path: "kv"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "k8s-cluster-b"
          role: "eso"
          serviceAccountRef:
            name: "external-secrets"
            namespace: "external-secrets"
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-server-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-server-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-b"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences:
            - "vault"
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-client-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-client-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-b"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences:
            - "vault"
EOF

# Check status on cluster A
kubectl get clustersecretstore,clusterissuer --context k3d-cluster-a

# Output example:
NAME                                                   AGE   STATUS   CAPABILITIES   READY
clustersecretstore.external-secrets.io/vault-backend   23h   Valid    ReadWrite      True

NAME                                               READY   AGE
clusterissuer.cert-manager.io/cnpg-client-issuer   True    23h
clusterissuer.cert-manager.io/cnpg-server-issuer   True    23h

# Check status on cluster B
kubectl get clustersecretstore,clusterissuer --context k3d-cluster-b

# Output example:
NAME                                                   AGE     STATUS   CAPABILITIES   READY
clustersecretstore.external-secrets.io/vault-backend   4m44s   Valid    ReadWrite      True

NAME                                               READY   AGE
clusterissuer.cert-manager.io/cnpg-client-issuer   True    4m44s
clusterissuer.cert-manager.io/cnpg-server-issuer   True    4m44s
```

