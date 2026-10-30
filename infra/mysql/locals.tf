locals {
  engine      = "aurora-mysql"
  engine_key  = "mysql"
  db_port     = 3306
  name_prefix = "vpc-lab-aurora-clone-boto3-${local.engine_key}"

  labs = {
    labprod = {
      role                = "labprod"
      vpc_name            = "${local.name_prefix}-prod"
      vpc_cidr            = "10.10.0.0/16"
      public_subnet_cidrs = ["10.10.0.0/24", "10.10.1.0/24"]
      cluster_identifier  = var.prod_cluster_identifier
      master_password     = var.prod_master_password
    }
    labdev = {
      role                = "labdev"
      vpc_name            = "${local.name_prefix}-dev"
      vpc_cidr            = "10.11.0.0/16"
      public_subnet_cidrs = ["10.11.0.0/24", "10.11.1.0/24"]
      cluster_identifier  = var.dev_cluster_identifier
      master_password     = var.dev_master_password
    }
  }

  common_tags = {
    Project     = "aurora-clone-boto3"
    Environment = "lab"
    Engine      = local.engine_key
    ManagedBy   = "terraform"
  }

  cleanup_extra_clusters_command = "python3 ${path.module}/../scripts/delete_extra_clusters.py --region ${var.aws_region} --prod ${var.prod_cluster_identifier} --dev ${var.dev_cluster_identifier} --temp ${var.temp_cluster_identifier} --old ${var.old_cluster_identifier}"
}
