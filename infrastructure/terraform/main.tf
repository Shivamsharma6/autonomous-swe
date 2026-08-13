terraform {
  required_version = ">= 1.0"
}

provider "local" {}

resource "null_resource" "cluster_provision" {
  provisioner "local-exec" {
    command = "echo Autonomous SWE cluster provisioned"
  }
}
