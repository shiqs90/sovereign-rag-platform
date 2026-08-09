resource "helm_release" "gpu_operator" {
  name             = "gpu-operator"
  repository       = "https://helm.ngc.nvidia.com/nvidia"
  chart            = "gpu-operator"
  version          = var.gpu_operator_chart_version
  namespace        = "gpu-operator"
  create_namespace = true
  timeout          = 900

  # Unlike EKS (driver baked into the accelerated AMI), AKS GPU nodes are created with
  # gpu_driver = "None" and the Operator installs the driver itself.
  set {
    name  = "driver.enabled"
    value = "true"
  }

  # AKS's base /etc/containerd/config.toml is version 2 and imports drop-ins from
  # /etc/containerd/conf.d/. By default the toolkit writes its NVIDIA runtime as a
  # drop-in with a HIGHER config version, and containerd 2.x refuses to start. Point the
  # toolkit at the main config so it edits that file in place — no version mismatch.
  # (Belt and braces with os_sku = "Ubuntu2204" in aks.tf.)
  set {
    name  = "toolkit.env[0].name"
    value = "CONTAINERD_CONFIG"
  }
  set {
    name  = "toolkit.env[0].value"
    value = "/etc/containerd/config.toml"
  }

  # Tolerate the custom GPU taint so the Operator's DaemonSets land on the GPU node.
  # Without this they never schedule and the node never advertises nvidia.com/gpu.
  set {
    name  = "daemonsets.tolerations[0].key"
    value = "nvidia.com/gpu"
  }
  set {
    name  = "daemonsets.tolerations[0].operator"
    value = "Exists"
  }
  set {
    name  = "daemonsets.tolerations[0].effect"
    value = "NoSchedule"
  }

  depends_on = [azurerm_kubernetes_cluster_node_pool.gpu]
}
