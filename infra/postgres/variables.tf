variable "aws_region" {
  description = "AWS region for the lab"
  type        = string
  default     = "us-east-1"
}

variable "prod_cluster_identifier" {
  description = "Aurora cluster / writer identifier for PROD"
  type        = string
  default     = "prod-aurora-pg"
}

variable "dev_cluster_identifier" {
  description = "Aurora cluster / writer identifier for DEV"
  type        = string
  default     = "dev-aurora-pg"
}

variable "temp_cluster_identifier" {
  description = "Clone TEMP cluster name (destroy hook; align with TEMP_IDENTIFIER in .env)"
  type        = string
  default     = "temp-aurora-pg"
}

variable "old_cluster_identifier" {
  description = "Post-rename OLD cluster name (destroy hook; align with OLD_IDENTIFIER in .env)"
  type        = string
  default     = "olddev-aurora-pg"
}

variable "master_username" {
  description = "Master username for Aurora clusters"
  type        = string
  default     = "labadmin"
}

variable "prod_master_password" {
  description = "Master password for PROD Aurora (required; must differ from dev_master_password)"
  type        = string
  sensitive   = true
  default     = null
  nullable    = true
}

variable "dev_master_password" {
  description = "Master password for DEV Aurora (required; must differ from prod_master_password)"
  type        = string
  sensitive   = true
  default     = null
  nullable    = true
}

variable "allowed_cidr" {
  description = "CIDR allowed to reach the Aurora writer (prefer your public IP/32)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "engine_version" {
  description = "Aurora PostgreSQL engine version (leave empty for AWS default)"
  type        = string
  default     = ""
}

variable "min_capacity" {
  description = "Serverless v2 minimum ACU (0 enables auto-pause)"
  type        = number
  default     = 0
}

variable "max_capacity" {
  description = "Serverless v2 maximum ACU"
  type        = number
  default     = 1
}

variable "seconds_until_auto_pause" {
  description = "Idle seconds before Serverless v2 auto-pause (only when min_capacity=0)"
  type        = number
  default     = 600
}
