locals {
  engine      = "aurora-mysql"
  engine_key  = "mysql"
  db_port     = 3306
  create_dev  = var.deploy_mode == "prod_dev"
  name_prefix = "vpc-lab-aurora-clone-boto3-${local.engine_key}"

  labs_all = {
    labprod = {
      role                = "labprod"
      vpc_name            = "${local.name_prefix}-prod"
      vpc_cidr            = "10.10.0.0/16"
      public_subnet_cidrs = ["10.10.0.0/24", "10.10.1.0/24"]
      cluster_identifier  = var.prod_cluster_identifier
    }
    labdev = {
      role                = "labdev"
      vpc_name            = "${local.name_prefix}-dev"
      vpc_cidr            = "10.11.0.0/16"
      public_subnet_cidrs = ["10.11.0.0/24", "10.11.1.0/24"]
      cluster_identifier  = var.dev_cluster_identifier
    }
  }

  labs = {
    for key, lab in local.labs_all : key => lab
    if key == "labprod" || local.create_dev
  }

  common_tags = {
    Project     = "aurora-clone-boto3"
    Environment = "lab"
    Engine      = local.engine_key
    ManagedBy   = "terraform"
  }
}
