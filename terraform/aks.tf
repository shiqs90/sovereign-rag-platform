resource "azurerm_resource_group" "this" {
  name     = "rg-${var.cluster_name}"
  location = var.location

  tags = {
    project = var.cluster_name
  }
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = var.cluster_name
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = var.cluster_name
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Free" # control plane costs $0 (no uptime SLA — fine for a demo)

  # Both upgrade channels OFF. AKS's default node-image channel performs a SURGE
  # upgrade (adds a node before draining the old one), which needs 2x the pool's vCPU.
  # With a 4-vCPU T4 quota that fails hard — the exact ErrCode_InsufficientVCPUQuota
  # already hit in llm-argo-canary. Over a multi-day cluster it can fire unattended.
  
  # automatic_upgrade_channel = "none"  # provider only accepts patch/rapid/stable/node-image; omitting it IS "off"
  node_os_upgrade_channel = "None"

  identity {
    type = "SystemAssigned"
  }

  # THE landmine of this project. Without a network_policy engine declared HERE, the
  # cluster ships with none — NetworkPolicy objects are then accepted by the API server
  # and silently never enforced (`kubectl get netpol` lists them; nothing happens).
  # The engine CANNOT be added to a running cluster; it requires recreation.
  #
  # Calico over Azure NPM: egress-rule behaviour is better documented and its failure
  # modes are more searchable, and this project writes default-deny egress policies.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "calico"
    pod_cidr            = "10.244.0.0/16"
  }

  # System pool — no GPU, no app workloads. Steered to by nothing; it's the default
  # landing zone for kube-system, ingress-nginx, cert-manager and the Operator controller.
  default_node_pool {
    name            = "system"
    vm_size         = var.system_vm_size
    node_count      = 1
    os_disk_size_gb = 64
    node_labels     = { pool = "system" }

    upgrade_settings {
      max_surge = "1"
    }
  }

  tags = {
    project = var.cluster_name
  }
}

# Apps pool — Qdrant, TEI, the two rag-services, Langfuse, Postgres.
#
# Deliberately NOT tainted. A taint here protects nothing (there is no scarce device to
# guard) and creates a Pending-forever failure mode if a platform pod lacks the
# toleration. Placement is steered from the workload side with `nodeSelector: pool=apps`.
resource "azurerm_kubernetes_cluster_node_pool" "apps" {
  name                  = "apps"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  vm_size               = var.apps_vm_size
  node_count            = 1
  os_disk_size_gb       = 64

  node_labels = {
    pool = "apps"
  }

  upgrade_settings {
    max_surge = "1"
  }

  tags = {
    project = var.cluster_name
  }
}

# GPU pool: 1x T4 serving the single vLLM pod.
# gpu_driver = "None" skips AKS's own NVIDIA driver install so the GPU Operator owns the
# full stack (driver + device plugin + toolkit) without conflicts.
resource "azurerm_kubernetes_cluster_node_pool" "gpu" {
  name                  = "gpu"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  vm_size               = var.gpu_vm_size
  node_count            = 1
  gpu_driver            = "None"

  # Pin to Ubuntu 22.04 (containerd 1.7). On the default 24.04 image (containerd 2.3),
  # the GPU Operator toolkit writes a version-4 containerd drop-in that AKS's version-2
  # root config rejects ("drop-in config version 4 higher than root config version 2"),
  # containerd fails to start -> kubelet down -> node NotReady in an auto-repair loop.
  # containerd 1.7 has no such version check and accepts the toolkit's config.
  os_sku = "Ubuntu2204"

  node_labels = {
    workload = "gpu"
  }

  # Unlike the apps pool, this taint IS load-bearing: it keeps every non-GPU pod off the
  # one scarce, expensive node.
  node_taints = ["nvidia.com/gpu=present:NoSchedule"]

  upgrade_settings {
    max_surge = "1"
  }

  tags = {
    project = var.cluster_name
  }
}
