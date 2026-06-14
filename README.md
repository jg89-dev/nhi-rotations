# NHI Rotation Script

## Background

Keeper's updated pricing model counts active PAM and KSM resources toward a Non-Human Identity (NHI) total. Any resource with active usage — Gateways, KSM devices, PAM machines, PAM users — contributes to this count, which is billed in large tier increments. The included base tier of 24 NHIs is a significant reduction from the previously unlimited model.

### PAM Machines

There's limited room to optimise NHI costs from machine connections and tunnels. The most effective option is enabling **Allow shared users to select their own host and credentials**, which lets you consolidate to one machine record per protocol (e.g. a single RDP template and a single SSH template). This comes at the cost of user experience and reduces content-level access control.  
<img width="290" height="75" alt="image" src="https://github.com/user-attachments/assets/47fc79eb-690a-4c97-a5a3-f195052844a9" />


### PAM Rotations

Rotations are where the real savings are. Since any PAM user with active rotation usage counts as an NHI, the trick is to route all rotations through a single "NHI" PAM user — then sync the result back to the original record. This script implements that pattern, reducing your rotation NHI footprint to just 1 regardless of how many accounts you're rotating.  
<img width="720" height="320" alt="password_rotation" src="https://github.com/user-attachments/assets/757e246c-7680-4e62-93d5-cc0bea45f335" />


---

## How It Works

One PAM user sits in your Gateway folder and acts as the sole NHI — the only record that actually rotates. Everything else is inert.

A bank of PAM user records in your chosen folders defines what gets rotated and when. Each has custom fields specifying the rotation config, resource, and a cron schedule. The script polls these records every minute and, when one is due:

1. Copies the target record's data onto the NHI PAM user
2. Rotates the NHI PAM user
3. Syncs the new credentials back to the target record

The target records never rotate directly — they just receive the result.  
Triggered rotations are added to a job queue, and both the CRON poll and rotation worker are individual threads, that way even if a rotation takes several seconds, other rotations will still be scheduled.

---

## Requirements

Each target record must be of type `pamUser` with the following custom fields (any field type):

| Field | Description |
|---|---|
| `rotation_config` | UID of the Gateway PAM Config |
| `rotation_resource` | UID of the rotation resource *(General profile only)* |
| `rotation_cron` | Quartz cron spec for the rotation schedule (e.g. `0 0 0/6 * * ?`) |

Records in the target folders that aren't `pamUser` type or are missing these fields will be silently skipped.

⚠️ **Note:** all target records must use the same rotation profile — either all with a `rotation_resource` (General) or all without (IAM). Commander cannot clear the rotation resource field once set.

---

## Setup

**1. Create the NHI PAM user**

This is the single record that will do all the rotating. All fields can be left blank except Login, which requires a value (a placeholder is fine).

**2. Create your target PAM users**

These are the inert `pamUser` records representing the accounts you actually want to rotate. Organise them into one or more folders. Add the custom fields above to each one. If you already have folders of `pamUser` records, you can point the script at those directly — it will ignore anything that doesn't have the required fields.

**3. (Optional) Create a dedicated Keeper user for the script**

This costs a licence but is recommended for audit purposes (see step 5). The user needs:
- Commander SDK access
- Permission to configure rotation and rotate records
- Edit access to both the NHI record and all target records

**4. Install and run the script**

```bash
# Create a directory and enter it
mkdir NHI_rotations && cd NHI_rotations

# Download NHI_rotations.py into this directory, then:

# Optional: set up a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install keepercommander croniter

# Run
python3 NHI_rotations.py
```

**5. Auditing**  

Because the inert records aren't actually rotated, there are no rotation logs for them. All logs are found on the NHI user, but that makes auditing difficult.

When viewing the target records' history, you'll find useful `Changed password` or other update events.  
For best results, run the script as a dedicated user (see step 3), as this will make it simple to track events in your Admin Console:  
`Updated record` event with a `Commander` Device from `{user}`

---

## Execution

On first launch the script will prompt for:
- The UID of your NHI PAM user
- A comma-separated list of folder names or UIDs containing your target records
- Keeper login credentials for the Commander session

These are saved to `NHI_rotations.json` and `commander_config.json` — subsequent runs are fully autonomous with persistent authentication.

Target records are loaded at startup. If you add records or change fields while the script is running, restart it to pick up the changes. To update the folder list, either edit `NHI_rotations.json` directly or delete it to be prompted again.

---

## ⚠️ Limitations

- Do not delete the NHI PAM user — this would require a new NHI to take its place
- PAM Scripts are not supported
- Granular password complexity rules are not supported
- SaaS rotations are not supported
- All target records must share the same rotation profile (include a `rotation_resource` in all or none)
