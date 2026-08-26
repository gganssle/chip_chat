"""The container registry, asserted as text.

The estate is applied against a real subscription, so these are the properties
worth catching before an apply rather than after one. Same approach as
``test_local_stack.py``: read the configuration, not the cloud.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "infra" / "terraform" / "registry.tf"
OUTPUTS = REPO_ROOT / "infra" / "terraform" / "outputs.tf"
VARIABLES = REPO_ROOT / "infra" / "terraform" / "variables.tf"


def test_the_registry_is_in_terraform_at_all() -> None:
    # Issue #103: "the registry is created by Terraform, not by hand." This
    # estate has already had to adopt one imperatively-created foundation.
    assert REGISTRY.is_file()
    assert 'resource "azurerm_container_registry" "main"' in REGISTRY.read_text()


def test_the_admin_account_is_disabled() -> None:
    # An admin account is a username and password with push rights, stored in
    # the registry. Nothing else in this estate runs on a credential like that.
    assert "admin_enabled = false" in REGISTRY.read_text()


def test_the_runtime_pulls_with_the_managed_identity() -> None:
    text = REGISTRY.read_text()
    assert '"AcrPull"' in text
    assert "azurerm_user_assigned_identity.app.principal_id" in text


def test_the_developer_can_push_for_the_local_build_path() -> None:
    # Subscription Owner does not imply registry push: it is a data action.
    text = REGISTRY.read_text()
    assert '"AcrPush"' in text
    assert "data.azurerm_client_config.current.object_id" in text


def test_the_login_server_is_an_output() -> None:
    # Nothing downstream should hardcode a name carrying a random suffix.
    text = OUTPUTS.read_text()
    assert 'output "container_registry_login_server"' in text
    assert 'output "agent_image_repository"' in text


def test_the_tier_is_the_cheap_one_by_default() -> None:
    # There is no free tier; Basic is ~$5/month against a $150 ceiling.
    text = VARIABLES.read_text()
    assert 'variable "container_registry_sku"' in text
    assert 'default     = "Basic"' in text
