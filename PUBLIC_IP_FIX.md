# Fix: `assign_public_ip` ignored during OCM plan deployment

## Summary

On the OCM plan page (`/ocm/plan`), the "Assign Public IP" checkbox value was
collected from the UI and stored in the plan's `vm_config_json`, but it was
never passed to OCI when the target asset was updated. As a result, deployed
instances did not receive a public IP even when the user selected it.

## Affected file

`app.py` — the block that applies per-VM custom configuration to each
`TargetAsset` via `clients['migration'].update_target_asset(...)`.

Locate the block by searching for:

```python
if 'shape' in config_data:
    user_spec = oci.cloud_migrations.models.LaunchInstanceDetails(
        shape=config_data['shape']
    )
```

(In the version this was patched against it was at approximately line 527.)

## Root cause

`config_data` (read from `vm_configs[source_id]`) contained `assign_public_ip`,
but the code that built `LaunchInstanceDetails` only set `shape`, `ocpus`, and
`memory_gb`. The `assign_public_ip` key was silently dropped. The
`update_target_asset` call therefore left networking at the OCI default, so the
deployed instance fell back to whatever the subnet default was — typically no
public IP.

The frontend (`templates/ocm_advanced.html`) correctly reads
`vm_public_ip_${vmId}.checked` and sends it as `assign_public_ip` inside
`vm_config.vm_configs`. The bug is strictly on the backend apply step.

## Fix

Attach a `CreateVnicDetails` to `user_spec` whenever `assign_public_ip` is
present in the per-VM config. Insert the new block immediately after the
existing `if 'shape' in config_data:` block and before `# Build update details`:

```python
if 'assign_public_ip' in config_data:
    if user_spec is None:
        user_spec = oci.cloud_migrations.models.LaunchInstanceDetails()
    user_spec.create_vnic_details = oci.cloud_migrations.models.CreateVnicDetails(
        assign_public_ip=bool(config_data['assign_public_ip'])
    )
    output.write(f"  - Assign Public IP: {bool(config_data['assign_public_ip'])}\n")
```

### Unified diff

```diff
                                             else:
                                                 output.write(f"  - Shape: {config_data['shape']}\n")

+                                        if 'assign_public_ip' in config_data:
+                                            if user_spec is None:
+                                                user_spec = oci.cloud_migrations.models.LaunchInstanceDetails()
+                                            user_spec.create_vnic_details = oci.cloud_migrations.models.CreateVnicDetails(
+                                                assign_public_ip=bool(config_data['assign_public_ip'])
+                                            )
+                                            output.write(f"  - Assign Public IP: {bool(config_data['assign_public_ip'])}\n")
+
                                         # Build update details
                                         update_details = oci.cloud_migrations.models.UpdateVmTargetAssetDetails(
                                             type='INSTANCE',
                                             user_spec=user_spec
                                         )
```

### Why `if user_spec is None:` guard

`user_spec` is only constructed inside the `if 'shape' in config_data:` branch.
If a future plan sends `assign_public_ip` without a shape override, the guard
ensures we still build a `LaunchInstanceDetails` to hang the VNIC details off.

## Behavior change worth noting

Previously, an unchecked "public IP" checkbox resulted in no VNIC override being
sent, so OCI used the subnet default. After the fix, an unchecked checkbox
sends `assign_public_ip=False` explicitly — which is the correct behavior for a
UI checkbox, but any users who relied on an implicit subnet-default public IP
will notice the change.

## Verification checklist

- [ ] Select "public IP" on a VM in `/ocm/plan`, deploy, confirm the instance
      has a public IP.
- [ ] Deselect "public IP" on a VM in `/ocm/plan`, deploy, confirm the instance
      has no public IP.
- [ ] Plan output log contains `- Assign Public IP: True/False` under the
      per-VM "Configuring target asset" section.

## SDK reference

- `oci.cloud_migrations.models.LaunchInstanceDetails.create_vnic_details`
- `oci.cloud_migrations.models.CreateVnicDetails(assign_public_ip=bool)`

Both verified present in the OCI Python SDK used by this project.
