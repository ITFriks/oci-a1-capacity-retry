"""The A1 size check reads the stack's Terraform the way the OCI Console writes it."""

from __future__ import annotations

import pytest
from fakes import console_tf, tf_zip

from oci_a1_retry.shape import A1Size, SizeUnknown, a1_size, format_amount


def size(files: dict[str, str], variables: dict[str, str] | None = None) -> A1Size | None:
    return a1_size(tf_zip(files), variables)


def test_console_stack_within_the_free_allowance():
    result = size(console_tf("2", "12"))
    assert result == A1Size(ocpus=2, memory_gb=12, instances=1)
    assert result.within_free_tier


def test_old_four_core_stack_is_over_the_allowance():
    result = size(console_tf("4", "24"))
    assert result == A1Size(ocpus=4, memory_gb=24, instances=1)
    assert not result.within_free_tier


def test_memory_alone_can_exceed_the_allowance():
    assert not size(console_tf("2", "16")).within_free_tier


def test_instances_are_added_up():
    files = {"a.tf": console_tf("1", "6")["main.tf"], "b.tf": console_tf("2", "12")["main.tf"]}
    assert size(files) == A1Size(ocpus=3, memory_gb=18, instances=2)


def test_unquoted_numbers_and_comments():
    tf = """
    # a comment with { an unbalanced brace
    resource "oci_core_instance" "vm" {
      shape = "VM.Standard.A1.Flex" // the free Arm shape
      /* shape_config { ocpus = 99 } */
      shape_config {
        ocpus         = 1
        memory_in_gbs = 6.5
      }
    }
    """
    assert size({"main.tf": tf}) == A1Size(ocpus=1, memory_gb=6.5, instances=1)


def test_variables_from_the_stack_win_over_defaults():
    tf = """
    variable "ocpus" { default = 4 }
    variable "memory" { default = "24" }
    variable "shape" { default = "VM.Standard.A1.Flex" }
    resource "oci_core_instance" "vm" {
      shape = var.shape
      shape_config {
        ocpus         = var.ocpus
        memory_in_gbs = "${var.memory}"
      }
    }
    """
    assert size({"main.tf": tf}) == A1Size(ocpus=4, memory_gb=24, instances=1)
    assert size({"main.tf": tf}, {"ocpus": "2", "memory": "12"}) == A1Size(2, 12, 1)


def test_other_shapes_are_ignored():
    tf = console_tf()["main.tf"].replace("VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro")
    assert size({"main.tf": tf}) is None


def test_braces_in_strings_and_heredocs_do_not_confuse_the_parser():
    tf = """
    resource "oci_core_instance" "vm" {
      display_name = "weird } name {"
      metadata = {
        user_data = <<EOT
    #cloud-config
    runcmd: [ "echo }" ]
    EOT
      }
      shape = "VM.Standard.A1.Flex"
      shape_config {
        ocpus = "2"
        memory_in_gbs = "12"
      }
    }
    """
    assert size({"main.tf": tf}) == A1Size(2, 12, 1)


@pytest.mark.parametrize(
    ("tf", "reason"),
    [
        (
            'resource "oci_core_instance" "vm" {\n shape = "VM.Standard.A1.Flex"\n}',
            "no shape_config",
        ),
        (
            'resource "oci_core_instance" "vm" {\n count = 2\n shape = "VM.Standard.A1.Flex"\n'
            " shape_config {\n ocpus = 1\n memory_in_gbs = 6\n }\n}",
            "count or for_each",
        ),
        (
            'resource "oci_core_instance" "vm" {\n shape = "VM.Standard.A1.Flex"\n'
            " shape_config {\n ocpus = var.cores\n memory_in_gbs = 6\n }\n}",
            "variable 'cores'",
        ),
        (
            'resource "oci_core_instance" "vm" {\n shape = "VM.Standard.A1.Flex"\n'
            " shape_config {\n ocpus = local.cores * 2\n memory_in_gbs = 6\n }\n}",
            "cannot evaluate",
        ),
        ('resource "oci_core_instance" "vm" {\n shape = "VM.Standard.A1.Flex"\n', "unclosed"),
    ],
)
def test_unclear_configurations_say_why(tf, reason):
    with pytest.raises(SizeUnknown, match=reason):
        size({"main.tf": tf})


def test_archives_that_are_not_terraform():
    with pytest.raises(SizeUnknown, match="not a readable zip"):
        a1_size(b"not a zip")
    with pytest.raises(SizeUnknown, match=r"no \.tf files"):
        size({"README.md": "hello"})


def test_format_amount():
    assert format_amount(2.0) == "2"
    assert format_amount(6.5) == "6.5"
