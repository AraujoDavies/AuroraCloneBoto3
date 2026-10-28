output "deploy_mode" {
  description = "Active deploy mode"
  value       = var.deploy_mode
}

output "prod_vpc_id" {
  description = "PROD lab VPC ID"
  value       = aws_vpc.lab["labprod"].id
}

output "prod_vpc_name" {
  description = "PROD lab VPC Name tag"
  value       = local.labs["labprod"].vpc_name
}

output "prod_cluster_identifier" {
  description = "PROD Aurora cluster identifier"
  value       = aws_rds_cluster.lab["labprod"].cluster_identifier
}

output "prod_endpoint" {
  description = "PROD Aurora cluster endpoint"
  value       = aws_rds_cluster.lab["labprod"].endpoint
}

output "prod_port" {
  description = "PROD Aurora port"
  value       = aws_rds_cluster.lab["labprod"].port
}

output "prod_db_subnet_group_name" {
  description = "PROD DB subnet group (override DB_SUBNET_GROUP only if TEMP must live in PROD VPC)"
  value       = aws_db_subnet_group.lab["labprod"].name
}

output "prod_security_group_id" {
  description = "PROD security group ID (override VPC_SECURITY_GROUP_IDS only if TEMP must live in PROD VPC)"
  value       = aws_security_group.aurora["labprod"].id
}

output "dev_vpc_id" {
  description = "DEV lab VPC ID (null when deploy_mode=prod)"
  value       = try(aws_vpc.lab["labdev"].id, null)
}

output "dev_vpc_name" {
  description = "DEV lab VPC Name tag (null when deploy_mode=prod)"
  value       = try(local.labs["labdev"].vpc_name, null)
}

output "dev_cluster_identifier" {
  description = "DEV Aurora cluster identifier (null when deploy_mode=prod)"
  value       = try(aws_rds_cluster.lab["labdev"].cluster_identifier, null)
}

output "dev_endpoint" {
  description = "DEV Aurora cluster endpoint (null when deploy_mode=prod)"
  value       = try(aws_rds_cluster.lab["labdev"].endpoint, null)
}

output "dev_port" {
  description = "DEV Aurora port (null when deploy_mode=prod)"
  value       = try(aws_rds_cluster.lab["labdev"].port, null)
}

output "dev_db_subnet_group_name" {
  description = "DEV DB subnet group — clone TEMP herda isto por padrão (null when deploy_mode=prod)"
  value       = try(aws_db_subnet_group.lab["labdev"].name, null)
}

output "dev_security_group_id" {
  description = "DEV security group ID — clone TEMP herda isto por padrão (null when deploy_mode=prod)"
  value       = try(aws_security_group.aurora["labdev"].id, null)
}

output "master_username" {
  description = "Master username"
  value       = var.master_username
}

output "db_port" {
  description = "Database port"
  value       = local.db_port
}
