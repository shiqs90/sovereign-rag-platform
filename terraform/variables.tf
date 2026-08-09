variable "location" {
  description = "Azure region. Must have approved NCASv3_T4 (T4 GPU) quota — the grant landed in australiacentral."
  type        = string
  default     = "australiacentral"
}

variable "cluster_name" {
  description = "AKS cluster name (also used for the resource group: rg-<name>)."
  type        = string
  default     = "sovereign-rag"
}

variable "kubernetes_version" {
  description = "AKS Kubernetes version. null = AKS default (`az aks get-versions --location <loc> -o table`)."
  type        = string
  default     = null
}

variable "system_vm_size" {
  description = "System pool. kube-system, ingress-nginx, cert-manager, GPU Operator controller. DSv3 — the family with verified quota in australiacentral (DASv5/DSv5 are 0)."
  type        = string
  default     = "Standard_D2s_v3"
}

variable "apps_vm_size" {
  description = "Apps pool. Qdrant, TEI, rag-services, Langfuse, Postgres. ~3.86 allocatable vCPU / ~12.6 GiB after AKS reservation."
  type        = string
  default     = "Standard_D4s_v3"
}

variable "gpu_vm_size" {
  description = "GPU pool. Standard_NC4as_T4_v3 = 1x NVIDIA T4 16GB. Quota is 4 vCPU = exactly one node."
  type        = string
  default     = "Standard_NC4as_T4_v3"
}

variable "gpu_operator_chart_version" {
  description = "NVIDIA GPU Operator chart version. v26.3.2 is the version proven working on this AKS/T4/Ubuntu2204 combination."
  type        = string
  default     = "v26.3.2"
}
