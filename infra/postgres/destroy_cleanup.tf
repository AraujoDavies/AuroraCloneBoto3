resource "terraform_data" "cleanup_extra_clusters" {
  input = local.cleanup_extra_clusters_command

  depends_on = [
    aws_rds_cluster.lab,
    aws_rds_cluster_instance.writer,
    aws_db_subnet_group.lab,
    aws_security_group.aurora,
    aws_vpc.lab,
  ]

  provisioner "local-exec" {
    when    = destroy
    command = self.input
  }
}
