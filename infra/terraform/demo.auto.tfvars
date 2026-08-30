# The live "demo" stack's non-default variables, loaded automatically.
#
# WHY THIS FILE IS COMMITTED, when `*.tfvars` is gitignored and the rest of the
# convention says configuration is per-operator.
#
# `snowflake_account_url` is not tuning. `databricks_publish.tf` gates four
# resources on it being non-empty -- `databricks_job.publish`,
# `databricks_job.publish_verify`, and the permission set on each -- through
# `count = ... && var.snowflake_account_url != "" ? 1 : 0`. Empty is the shipped
# default, and it is the right default for a fresh stack: a nightly job pointed
# at no account would fail on its first scheduled run and email somebody about a
# system that was never stood up.
#
# But the live stack WAS applied with it set. So on this stack the default is not
# "do not create the job", it is "DESTROY the job that exists" -- and since
# `*.tfvars` was gitignored there was no shared place to keep the value. Every
# operator had to independently know to write their own file, and an untargeted
# `terraform apply` from a fresh clone planned four to destroy. That is a trap
# that documentation had already failed to prevent: docs/nightly-publish.md §5
# step 3 has described it in prose the whole time.
#
# `*.auto.tfvars` is loaded without `-var-file`, so carrying it in the repository
# makes the safe apply the DEFAULT one rather than the one you have to remember.
#
# NOTHING HERE IS A SECRET, and nothing here may become one. This value is a
# hostname that `terraform.tfvars.example` -- also committed -- already prints in
# a comment. The publisher's private key is deliberately not a Terraform variable
# at all: Terraform creates the secret scope empty and an operator fills it, so
# no credential enters state. If a future variable belongs in this file but is
# sensitive, it belongs in Key Vault instead and this file gets a comment saying
# where it went.
#
# A scratch stack overrides this the ordinary way, and should:
#
#   terraform apply -var environment=scratch -var snowflake_account_url=""
#
# AWS us-east-2, not Azure East US 2 -- fixed when the trial was created and not
# changeable. GitHub #104 has that decision and what it costs.

snowflake_account_url = "hq72718.us-east-2.aws.snowflakecomputing.com"
