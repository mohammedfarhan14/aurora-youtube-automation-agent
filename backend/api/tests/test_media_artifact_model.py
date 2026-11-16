from sqlalchemy import UniqueConstraint

from app.models.media_artifact import MediaArtifact


def test_media_artifact_is_bound_to_tenant_run_and_stage():
    foreign_keys = {
        tuple(element.parent.name for element in constraint.elements): tuple(
            element.target_fullname for element in constraint.elements
        )
        for constraint in MediaArtifact.__table__.foreign_key_constraints
    }

    assert foreign_keys[("production_run_id", "organization_id")] == (
        "production_runs.id",
        "production_runs.organization_id",
    )
    assert foreign_keys[("production_stage_id", "production_run_id")] == (
        "production_stages.id",
        "production_stages.production_run_id",
    )


def test_media_artifact_storage_identity_is_unique_per_tenant():
    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in MediaArtifact.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("organization_id", "storage_backend", "storage_key") in unique_constraints


def test_media_artifact_metadata_does_not_store_binary_payloads():
    column_names = set(MediaArtifact.__table__.columns.keys())

    assert "storage_key" in column_names
    assert "sha256" in column_names
    assert "byte_size" in column_names
    assert "content" not in column_names
    assert "data" not in column_names
