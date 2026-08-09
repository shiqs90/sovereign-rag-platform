terraform {
  required_version = "~> 1.15"

  # HCP Terraform backend, LOCAL execution so the local `az login` session supplies
  # Azure credentials. Create the workspace in Shikha_Projects and set Execution Mode
  # to "Local" BEFORE the first apply.
  cloud {
    organization = "Shikha_Projects"

    workspaces {
      name = "sovereign-rag-platform"
    }
  }

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
