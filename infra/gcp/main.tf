terraform {
  required_version = ">= 1.5.0, < 2.0.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "6.50.0"
    }
  }
  backend "local" {}
}

provider "google" {
  project = var.project_id
  region  = "us-central1"
}

variable "project_id" {
  type = string
}
variable "service_name" {
  type    = string
  default = "codex-airlift"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,38}[a-z0-9]$", var.service_name))
    error_message = "Use a lowercase service name with 2–40 characters."
  }
}
variable "ssh_public_key" {
  type = string
  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$", trimspace(var.ssh_public_key)))
    error_message = "Provide one Ed25519 public key."
  }
}
variable "codex_version" {
  type = string
  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.codex_version))
    error_message = "Pin a numeric Codex release."
  }
}
variable "monthly_compute_budget_usd" {
  type    = number
  default = 100
  validation {
    condition     = var.monthly_compute_budget_usd >= 94 && var.monthly_compute_budget_usd <= 100
    error_message = "The pinned host requires $94–$100 per calendar month at the conservative rate ceiling."
  }
}
variable "budget_started_at" {
  type = string
}
variable "termination_time" {
  type = string
}

resource "terraform_data" "lease" {
  input = {
    started_at       = var.budget_started_at
    termination_time = var.termination_time
    monthly_budget   = var.monthly_compute_budget_usd
  }
  lifecycle {
    ignore_changes = [input]
    precondition {
      condition     = timecmp(var.termination_time, timeadd(var.budget_started_at, "2160h")) <= 0 && timecmp(var.termination_time, var.budget_started_at) > 0
      error_message = "Price-validation lease must be positive and no longer than 90 days."
    }
  }
}

resource "google_project_service" "api" {
  for_each           = toset(["compute.googleapis.com", "iap.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "host" {
  name                    = var.service_name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.api]
}
resource "google_compute_subnetwork" "host" {
  name          = var.service_name
  ip_cidr_range = "10.87.0.0/24"
  region        = "us-central1"
  network       = google_compute_network.host.id
}
resource "google_compute_firewall" "iap_ssh" {
  name          = "${var.service_name}-iap-ssh"
  network       = google_compute_network.host.name
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [var.service_name]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}
resource "google_service_account" "host" {
  account_id   = var.service_name
  display_name = "Codex Airlift host (no project roles)"
}
resource "google_compute_disk" "home" {
  name  = "${var.service_name}-home"
  zone  = "us-central1-a"
  type  = "pd-standard"
  size  = 100
  image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
  labels = {
    service = var.service_name
  }
  lifecycle {
    ignore_changes  = [image]
    prevent_destroy = true
  }
  depends_on = [google_project_service.api]
}
resource "google_compute_instance" "host" {
  name                      = var.service_name
  zone                      = "us-central1-a"
  machine_type              = "e2-highmem-2"
  allow_stopping_for_update = true
  deletion_protection       = true
  tags                      = [var.service_name]
  labels = {
    service                = var.service_name
    monthly_compute_budget = tostring(var.monthly_compute_budget_usd)
  }
  boot_disk {
    source      = google_compute_disk.home.id
    auto_delete = false
  }
  network_interface {
    subnetwork = google_compute_subnetwork.host.id
    access_config {}
  }
  service_account {
    email  = google_service_account.host.email
    scopes = []
  }
  metadata = {
    "block-project-ssh-keys" = "TRUE"
    "enable-oslogin"         = "FALSE"
    "ssh-keys"               = "codex:${trimspace(var.ssh_public_key)}"
    "airlift-termination"    = terraform_data.lease.output.termination_time
  }
  metadata_startup_script = templatefile("${path.module}/startup.sh.tftpl", {
    codex_version = var.codex_version
  })
  scheduling {
    provisioning_model          = "STANDARD"
    automatic_restart           = false
    instance_termination_action = "STOP"
    termination_time            = terraform_data.lease.output.termination_time
  }
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
}

output "host" {
  value = {
    name                      = google_compute_instance.host.name
    zone                      = google_compute_instance.host.zone
    machine_type              = google_compute_instance.host.machine_type
    termination_time          = terraform_data.lease.output.termination_time
    monthly_compute_budget    = var.monthly_compute_budget_usd
    conservative_monthly_cost = 744 * 0.125
    ssh_user                  = "codex"
  }
}
