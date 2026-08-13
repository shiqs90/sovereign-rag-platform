output "cluster_name" {
  value = azurerm_kubernetes_cluster.this.name
}

output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "configure_kubectl" {
  description = "Run this to point kubectl at the cluster."
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.this.name} --name ${azurerm_kubernetes_cluster.this.name} --overwrite-existing"
}

output "gpu_pool_scale" {
  description = "Cost control. Scale the GPU pool to 0 when not actively working."
  value       = "az aks nodepool scale --resource-group ${azurerm_resource_group.this.name} --cluster-name ${azurerm_kubernetes_cluster.this.name} --name gpu --node-count 0"
}

output "acr_login_server" {
  description = "Image prefix. Export as ACR and substitute into the k8s manifests."
  value       = azurerm_container_registry.this.login_server
}

output "acr_login" {
  description = "Authenticate the local docker CLI against the registry."
  value       = "az acr login --name ${azurerm_container_registry.this.name}"
}

output "network_policy_engine" {
  description = "Confirms NetworkPolicies will actually be ENFORCED, not silently ignored."
  value       = azurerm_kubernetes_cluster.this.network_profile[0].network_policy
}
