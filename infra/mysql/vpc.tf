data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "lab" {
  for_each = local.labs

  cidr_block           = each.value.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(local.common_tags, {
    Name = each.value.vpc_name
    Role = each.value.role
  })
}

resource "aws_internet_gateway" "lab" {
  for_each = local.labs

  vpc_id = aws_vpc.lab[each.key].id

  tags = merge(local.common_tags, {
    Name = "${each.value.vpc_name}-igw"
    Role = each.value.role
  })
}

resource "aws_subnet" "public" {
  for_each = {
    for pair in flatten([
      for role, lab in local.labs : [
        for idx, cidr in lab.public_subnet_cidrs : {
          key  = "${role}-${idx}"
          role = role
          idx  = idx
          cidr = cidr
        }
      ]
    ]) : pair.key => pair
  }

  vpc_id                  = aws_vpc.lab[each.value.role].id
  cidr_block              = each.value.cidr
  availability_zone       = data.aws_availability_zones.available.names[each.value.idx]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name = "${local.labs[each.value.role].vpc_name}-public-${each.value.idx}"
    Role = each.value.role
  })
}

resource "aws_route_table" "public" {
  for_each = local.labs

  vpc_id = aws_vpc.lab[each.key].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.lab[each.key].id
  }

  tags = merge(local.common_tags, {
    Name = "${each.value.vpc_name}-public-rt"
    Role = each.value.role
  })
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public[split("-", each.key)[0]].id
}

resource "aws_security_group" "aurora" {
  for_each = local.labs

  name        = "${each.value.vpc_name}-aurora"
  description = "Aurora MySQL lab access"
  vpc_id      = aws_vpc.lab[each.key].id

  ingress {
    description = "MySQL/Aurora"
    from_port   = local.db_port
    to_port     = local.db_port
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${each.value.vpc_name}-aurora-sg"
    Role = each.value.role
  })
}

resource "aws_db_subnet_group" "lab" {
  for_each = local.labs

  name = "${each.value.vpc_name}-subnets"
  subnet_ids = [
    for key, subnet in aws_subnet.public : subnet.id
    if startswith(key, "${each.key}-")
  ]

  tags = merge(local.common_tags, {
    Name = "${each.value.vpc_name}-subnets"
    Role = each.value.role
  })
}
