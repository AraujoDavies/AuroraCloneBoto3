output "deploy_mode" {
  description = "Active deploy mode"
  value       = var.deploy_mode
}

output "prod_vpc_id" {
  description = "PROD lab VPC ID"
  value       = aws_vpc.lab["prod"].id
}

output "prod_vpc_name" {
  description = "PROD lab VPC Name tag"
  value       = local.labs["prod"].vpc_name
}

output "prod_cluster_identifier" {
  description = "PROD Aurora cluster identifier"
  value       = aws_rds_cluster.lab["prod"].cluster_identifier
}

output "prod_endpoint" {
  description = "PROD Aurora cluster endpoint"
  value       = aws_rds_cluster.lab["prod"].endpoint
}

output "prod_port" {
  description = "PROD Aurora port"
  value       = aws_rds_cluster.lab["prod"].port
}

output "prod_db_subnet_group_name" {
  description = "PROD DB subnet group (use as DB_SUBNET_GROUP for TEMP in this VPC)"
  value       = aws_db_subnet_group.lab["prod"].name
}

output "prod_security_group_id" {
  description = "PROD security group ID (use in VPC_SECURITY_GROUP_IDS)"
  value       = aws_security_group.aurora["prod"].id
}

output "dev_vpc_id" {
  description = "DEV lab VPC ID (null when deploy_mode=prod)"
  value       = try(aws_vpc.lab["dev"].id, null)
}

output "dev_vpc_name" {
  description = "DEV lab VPC Name tag (null when deploy_mode=prod)"
  value       = try(local.labs["dev"].vpc_name, null)
}

output "dev_cluster_identifier" {
  description = "DEV Aurora cluster identifier (null when deploy_mode=prod)"
  value       = try(aws_rds_cluster.lab["dev"].cluster_identifier, null)
}

output "dev_endpoint" {
  description = "DEV Aurora cluster endpoint (null when deploy_mode=prod)"
  value       = try(aws_rds_cluster.lab["dev"].endpoint, null)
}

output "dev_port" {
  description = "DEV Aurora port (null when deploy_mode=prod)"
  value       = try(aws_rds_cluster.lab["dev"].port, null)
}

output "dev_db_subnet_group_name" {
  description = "DEV DB subnet group (null when deploy_mode=prod)"
  value       = try(aws_db_subnet_group.lab["dev"].name, null)
}

output "dev_security_group_id" {
  description = "DEV security group ID (null when deploy_mode=prod)"
  value       = try(aws_security_group.aurora["dev"].id, null)
}

output "master_username" {
  description = "Master username"
  value       = var.master_username
}

output "db_port" {
  description = "Database port"
  value       = local.db_port
}
