# Silkroad Blender 5 Skill/VFX Importer

Blender 5 addon for studying and recreating Silkroad Online character resources, animations, weapons, and skill VFX from an extracted client folder.

The importer is focused on research and visualization. It does not include any Silkroad game files, PK2 archives, models, textures, or other proprietary assets.

> **Work in progress — skill playback is not yet 100% game-accurate.** Timing, projectiles, attachments, buffs, and some VFX are still being investigated and improved. Use the current skill preview for research, not as a definitive reproduction of in-game behavior.

## Highlights

- Imports Silkroad `.bsr` resources with meshes, materials, skeletons, and animation tables.
- Imports `.bms`, `.bsk`, `.ban`, `.bmt`, `.ddj`, and `.efp` data used by the supported resource flow.
- Plays skills from `Media/server_dep/silkroad/textdata/skilleffect.txt`.
- Applies matching character `.ban` animations when the selected character supports the skill animation group.
- Imports visual `.efp` effects as Blender proxy objects.
- Supports character and weapon presets through a local JSON config.
- Attaches weapons/items to character bones such as `Bip01 R Hand`.
- Uses diffuse texture alpha by default for imported model materials and exposes a roughness control (default `1.0`). In Blender 5, alpha materials use the Dithered render method.
- Optional skill cue offsets:
  - reads `READY`, `WAIT`, `SHOT`, `ACT_S`, `ACT_L`, and related cue rows from `skilleffect.txt`;
  - reads attach modes such as `AT_MOV_1TAR`, `AT_ONE_FOLLOW`, and `AT_LOOP`;
  - reads movement modes such as `MOV_STRAIGHT`, `MOV_UPR`, `MOV_PIERCE`, and `MOV_NONE`;
  - reads cue bones like `Bip01 R Hand`, `Bip01 L Hand`, and `ai_end`;
  - imports projectile/cue resources such as `cha_arrow_normal.bsr`;
  - can aim projectile cues toward a selected scene object through the `TARGET` field.

## Requirements

- Blender 5.0 or newer.
- An extracted Silkroad client folder containing `Data`, `Media`, and `Particles`.
- Python packages bundled with Blender are enough for the addon itself.

Tested locally with Blender 5.0.1.

## Installation

1. Download or clone this repository.
2. In Blender, open `Edit > Preferences > Add-ons`.
3. Choose `Install from Disk`.
4. Select `silkroad_blender5_skill_importer.py`.
5. Enable `Silkroad Skill/VFX Importer`.
6. Open the `Silkroad` tab in the 3D View sidebar.

## Basic Workflow

1. Set `Game Root` to the extracted client folder.
2. Load or register a character preset.
3. Choose a weapon type and equip/register a weapon if needed.
4. Pick a skill in `Skill Player`.
5. Press `Play Selected Skill`.

By default the importer keeps effects centered in the stable legacy behavior. Enable `Use Skill Offsets` only when you want the importer to use cue bones, cue offsets, and projectile movement from `skilleffect.txt`.

## Skill Offsets And TARGET

`Use Skill Offsets` is optional and disabled by default.

When enabled, the importer reads extra cue rows from `skilleffect.txt`. For example, Pacheon/Bow skills often contain rows that say the projectile should start at `Bip01 R Hand` and move using `AT_MOV_1TAR` with `MOV_STRAIGHT`, `MOV_UPR`, or `MOV_PIERCE`.

If `TARGET` is assigned to an object in the scene, moving cues aim and travel toward that object. If no `TARGET` is assigned, the importer uses the cue movement data as a forward preview guide.

Imported cue objects store useful custom properties, including:

- `silkroad_skill_code`
- `silkroad_attach_type`
- `silkroad_move_type`
- `silkroad_attach_bone`
- `silkroad_start_offset`
- `silkroad_target_offset`
- `silkroad_target`

These are useful when inspecting how a skill is structured.

## Notes

- The addon is a research importer, not a full recreation of the Silkroad runtime.
- Some effects are approximated as Blender proxy meshes/billboards.
- Projectile and target behavior is intended for study and preview, not exact combat simulation.
- Game assets are intentionally ignored by Git and are not part of this repository.

## Repository Contents

- `silkroad_blender5_skill_importer.py` - the Blender addon.
- `README.md` - this guide.
- `.gitignore` - excludes extracted clients, PK2 archives, caches, and local generated files.

## License

MIT. See `LICENSE`.
