# Registry names are globally unique across all of Azure and must be alphanumeric only —
# no hyphens. A random suffix avoids collisions without hand-editing.
resource "random_string" "acr" {
  length  = 6
  special = false
  upper   = false
}

resource "azurerm_container_registry" "this" {
  # "sovereign-rag" with the hyphen stripped, plus the suffix -> sovereignrag<6 chars>.
  name                = "${replace(var.cluster_name, "-", "")}${random_string.acr.result}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Basic" # ~$5/month, prorated. Geo-replication is a Premium feature we don't need.
  admin_enabled       = false   # no static username/password; pulls go through managed identity

  tags = {
    project = var.cluster_name
  }
}

# The pull permission, granted to the cluster's KUBELET identity — not the cluster identity.
#
# AKS has two identities and confusing them is the usual failure here:
#   - the CLUSTER identity manages Azure resources (load balancers, disks)
#   - the KUBELET identity is what nodes use to pull images
# An AcrPull role assigned to the wrong one produces ImagePullBackOff with a 401.
#
# This is what `az aks update --attach-acr` does under the hood. Declaring it here keeps
# the grant in state instead of as an undocumented imperative step.
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                            = azurerm_container_registry.this.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}
