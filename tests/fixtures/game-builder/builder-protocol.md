# Builder protocol

The stage-by-stage runbook for building the game. The Orchestrator follows all of it and hands each role only the parts that apply to it. The rubric is in `visual-bar.md` and the Critic's prompt is in `critic-prompt.md`, both next to this file.

## Contents

- Stage gates
- Stage A: Brief, refs, plan, abstractions
- Stage B: Playable foundation
- Stage C: Art pipeline (sub-gates C0 to C5, generation pass protocol, camera-critical characters)
- Escalation ladder
- Stuck-loop diagnosis
- Stage D: Capture and Critic loop
- Stage E: Comparison matrix
- Critic contract
- Deliverables
- Pre-handoff verification
- Failure modes
- Handoff template

## Stage gates

Each stage lists Entry, Work, Exit checklist, and Gate owner. Do not start a stage before the previous Exit checklist is fully checked. Record every gate result in `artifacts/rounds.log`.

## Stage A: Brief, refs, plan, abstractions

Entry: the user's request.

Work:

1. Write `BRIEF.md` (one page): original working title, genre loop, camera and space model, session length target, win and fail conditions, non-goals, platform and stack constraints from the user, deliverables list, `Publish: LOCKED`.
2. Lock refs. Research real shipped-game screenshots in the target genre and UI style. Save 4 to 8 images to `refs-locked/ref-NN-<slug>.<ext>`. Cover four slots: wide gameplay shot, mid-distance action, HUD-heavy frame, environment or lighting mood. Prefer native 1080p or higher captures of shipped titles. Write `refs-locked/SOURCES.md`: file, title, source URL, retrieval date, slot covered, and the note "review only, not shipped".
3. Freeze refs. After Stage B, a ref may change only through a logged `REF-SWAP` with a reason, never to a weaker ref, and the Critic re-scores from scratch.
4. Choose a stack that can reach the bar. Requirements: real-time cast shadows, PBR-style materials with normal and roughness inputs, ambient occlusion or equivalent, a post-processing stack (tonemapping, grade, bloom, fog), instancing for density, and a HUD layer that can carry designed typography and panels. If the refs are 3D, a 2D canvas or sprite-only stack fails this test before coding starts.
5. Dispatch the Planner. `PLAN.md` must answer: genre loop and pacing; camera and space model; entity and component boundaries; data flow (input to command to simulation to render); win and fail conditions; stable vs swappable layers (art, levels, HUD skin); renderer contract (what the renderer needs from the world: materials, lights, shadow casters, post stack, HUD data); art plan (look target per ref, lighting rig, material families, density strategy, HUD grammar); risk list with escalation triggers. If any section is thin, send it back before coding.
6. Implement core abstractions: world or scene graph, entity update loop, input to command to state, renderer contract, camera, fog and HUD hooks. Compile and smoke-run one scene.
7. Create `art/LEDGER.md` v0: every visible thing in the plan, its type, intended source (generation pass, procedural, authored), status `placeholder`, and the ref it targets.
8. Lock `art/BAR.md` following `visual-bar.md`: universal core (C1 to C5) plus any game-specific criteria justified by the locked refs. Total criteria must be 5 to 15, numbered C1..Cn. Include PASS when, FAIL signs, justifying ref ids, and `core` or `game` for each.
9. Recommended: Critic plan review. The Critic reads `PLAN.md`, `art/BAR.md`, and the refs and returns `VERDICT: PLAN-OK` or `VERDICT: PLAN-GAPS` with numbered gaps in the path to the locked Visual Bar. It is the cheapest round you will ever run.

Exit checklist:

- [ ] `BRIEF.md` frozen with `Publish: LOCKED`
- [ ] `refs-locked/` has 4 to 8 refs covering all four slots, plus `SOURCES.md`
- [ ] Stack meets the renderer capability requirements
- [ ] `PLAN.md` answers every required section, including the art plan and escalation triggers
- [ ] `art/BAR.md` locked with 5 to 15 criteria (core C1 to C5, plus game-specific criteria as justified by the refs)
- [ ] Abstractions compile and a smoke run ticks the loop in one scene
- [ ] `art/LEDGER.md` v0 covers every planned visible element
- [ ] No model identifiers in any file

Gate owner: Orchestrator. Critic plan review recommended.

## Stage B: Playable foundation

Entry: Stage A exit.

Work:

- The Builder implements the core loop with placeholders. Placeholders are allowed here and nowhere later.
- Boot path: clean clone, install, build, run, exit 0. Document it in `README.md`.
- Camera model final. Art density is judged at this camera distance later, so lock it now against the refs.
- Input, win and fail conditions, and HUD data hooks functional. HUD unstyled but bound to real state.
- Renderer contract proven: materials, lights, shadows, and post stack swappable without touching gameplay code.
- Evidence for the Critic (files only): `artifacts/stage-b/boot.log` (command and exit code) and `artifacts/stage-b/loop-frames/` (8 to 12 frames from start to an end state, camera as locked).

Exit checklist:

- [ ] Boot documented and verified exit 0 at a recorded commit
- [ ] Loop playable from start to a win or fail state
- [ ] Camera matches the brief and the refs' typical distance and angle
- [ ] Renderer contract swap tested (replace one material set and one light without gameplay edits)
- [ ] Critic returns `VERDICT: B-PASS (loop only, NOT A VISUAL WIN)` or FAIL with a punch list on loop, boot, or camera, from the stage-b evidence

Gate owner: Critic, loop-only rubric. The verdict string must contain `NOT A VISUAL WIN`.

## Stage C: Art pipeline (staged, gated)

Entry: Stage B exit.

Art is not one pass after placeholders. Run the sub-gates in order. After each sub-gate, re-verify boot and loop, update `art/LEDGER.md`, and get a fresh Critic art check (`VERDICT: ART-PASS C<n>` or FAIL with a punch list). Each art check uses interim captures made with the Stage D capture protocol (stills matched to refs, SxS with blur variants, walkthrough frames for C5), saved under `artifacts/stage-c/C<n>/`. Lighting comes first because it is the cheapest lever and it multiplies every asset that follows.

Sub-gates C0 to C5 are pipeline steps. Criterion ids in `art/BAR.md` also start at C1, so each gate below names the criterion its art check scores.

C0. Look decomposition. For each ref, write one paragraph in `art/LOOK.md`: palette, key light direction and color, fill and ambient, sky, fog, material families present (metal, stone, cloth, foliage, skin, glass), ground treatment, prop density, HUD grammar, post effects (bloom, grade, vignette, depth of field, AO). This is the basis for generation prompts and the Critic's checklist.
Gate: every ref has a paragraph. The Orchestrator checks.

C1. Lighting and post rig, placeholders still in. Directional key with cast shadows, ambient or image-based fill, AO, tonemapping, color grade, bloom, fog. Tune against `LOOK.md`.
Gate: the placeholder scene, blurred SxS against the matched ref, already reads in the ref's value structure and light direction. Critic art check on criteria C1 (materials and lighting) and C3 (SxS blur test).

C2. Materials. Replace every placeholder material with a material set (albedo, roughness, normal, optional AO and height) produced by generation passes or procedural generation. Break tile repetition with detail layers, decals, and variation. Ground reads as surface, not plane.
Gate: no flat single-color surface in any planned camera. Critic art check on criterion C1 (materials and lighting).

C3. Geometry density. Hero units, props, terrain silhouettes, instanced scatter, debris, decals. LODs if performance requires. Match silhouette count and detail to the ref at the locked camera distance. Only use 3D if needed: if the locked refs are 2D or pixel art (or 2D can hit the bar), use sprites and atlases and do not force GLB. When the refs lock 3D for a hero or camera-tracked character, go Blender-first with the companion skill `game-builder-blender-assets` (GLB plus a turnaround gate before gait retarget). Do not ship procedural primitive 3D heroes as final.
Gate: no primitive stands in for an object in any planned camera, and the silhouette range matches the matched ref crops. Critic art check on criterion C2 (geometry and silhouette density).

C4. HUD art. Frame and chrome, panels, icon set, typeface with hierarchy, text contrast treatment, every HUD state in the brief (health, resources, minimap, objectives, prompts as applicable). Fonts must be original or licensed for redistribution.
Gate: if `art/BAR.md` includes a HUD or UI criterion, the HUD over the live frame reads as game UI in the ref grammar at 1080p and at half scale. Critic art check on that criterion. If no HUD criterion is locked, skip C4 and log `C4 skipped (no HUD criterion)`.

C5. Motion polish. Animation states, hit and ability VFX, particles, camera feel. Every animation state uses final assets.
Gate: a 30 second play session shows no placeholder, no missing texture, no flicker. Critic art check on criterion C5 (walkthrough consistency) using extracted frames, plus any motion-related game-specific criteria.

Exit checklist:

- [ ] `art/LOOK.md` complete
- [ ] `ART-PASS C1` through `ART-PASS C5` recorded in `rounds.log` (or `C4 skipped` when no HUD criterion is locked)
- [ ] `art/LEDGER.md` has zero `placeholder` rows and every row lists its source
- [ ] `art/GENERATION-LOG.md` records every generation (prompt, output file, status) with no franchise terms and no model identifiers
- [ ] Boot and loop re-verified at the current commit

Gate owner: Critic per sub-gate. The Orchestrator checks the ledger.

### Generation pass protocol

1. Write prompts from `LOOK.md` vocabulary and the brief's original names. Never include franchise names, character names, studio names, or "in the style of <game>". Never feed ref images as image-to-image or style input.
2. Generate in batches by material family or asset class.
3. Key, clean, and convert to engine-ready assets: tileable where needed, power-of-two sizes, derived or generated roughness and normal maps, consistent texel density.
4. Import, light-check in-engine under the C1 rig, and compare against the matched ref.
5. Update `art/LEDGER.md` (status `draft` or `final`) and `art/GENERATION-LOG.md`.
6. Iterate until the in-engine check passes. Several passes per asset class are expected.

### Camera-critical characters (3D only if needed)

Only use 3D if needed. If the locked refs are 2D or pixel art, or a 2D sprite path can hit the bar, use sprites, atlases, or generated 2D art. Do not start a Blender or GLB pass by default.

When, and only when, the locked refs demand a 3D walking hero or close NPC, do not author the final mesh as in-engine primitives (boxes, cylinders, debug foot discs, floating eye spheres). That path produces shippable gait on unshippable characters. Turnaround and hero visual gates belong to the Orchestrator; do not ask another agent for the PASS or FAIL.

1. Run the companion skill `game-builder-blender-assets`.
2. Author mesh, UVs, and a simple humanoid rig in Blender in isolation; export GLB; pass the turnaround gate; then bind the existing gait system and delete the placeholder hero.
3. In-engine procedural geometry and generation passes stay fine for props, crops, distant NPCs, billboards, and greybox while a justified Blender hero is in flight.
4. Stage D punches that say "fix the mannequin in the renderer" escalate to that companion skill only if the game is 3D; otherwise escalate to sprite art. If uncertain, ask the Orchestrator and prefer 2D.

## Escalation ladder

Trigger: the same criterion fails two consecutive Critic checks with no visible change in the matched SxS, or the Builder reports that a criterion cannot be reached on the current rung. Escalate one rung. Log `ESCALATE rung <n>` with the reason. Never lower the bar.

1. Lighting and post: fix or add shadows, AO, tonemapping, grade, fog, bloom.
2. Material fidelity: normal and roughness maps, detail textures, decals, breakup.
3. Density: instanced scatter, more props, terrain displacement, debris, LODs.
4. Renderer features: move to a pipeline with deferred or clustered lighting, shadow cascades, screen-space AO, and a full post stack. Move from 2D canvas or sprite rendering to a 3D pipeline if the refs are 3D.
5. Stack change: if the engine cannot render the refs' features, switch engines. The renderer contract from Stage A keeps gameplay intact.

The Builder proposes a rung. The Orchestrator decides. The Critic never prescribes stack or implementation. After rung 4 or 5, re-run the C1 to C5 gates before returning to Stage D.

## Stuck-loop diagnosis

Trigger (any one is enough):

- 3 or more consecutive Stage D hard-gate `FAIL` verdicts on the same game with the same failing criteria family from `art/BAR.md` (for example SxS blur, density, or one game-specific criterion repeating).
- A RECAPTURE streak of 2 or more with no visible change in the matched SxS blur pairs.
- The Builder keeps shipping the same class of fix (chrome rails, plate overlays, one more prop) while the Critic's punch list stays structurally identical.

When triggered, the Orchestrator must:

1. Pause further Builder punches for that game. Log `DIAGNOSE hold` in `artifacts/rounds.log`. Keep the playable preview up. Publish stays LOCKED.
2. Spawn a Diagnoser as a fresh third agent, in a separate session from the Builder and the Critic. Give it files only: recent `artifacts/verdicts/`, `artifacts/rounds.log`, current stills and SxS (with blur variants), `BRIEF.md`, `art/LOOK.md`, the Visual Bar (`art/BAR.md` plus `visual-bar.md` or `critic-prompt.md`), and short extracts of Builder handbacks. Do not paste Builder chat into the Critic. Do not ask the Diagnoser to soft-pass.
3. The Diagnoser writes `artifacts/diagnosis/R<n>-steer.md` with exactly:
   - Root cause: why the loop is not progressing (wrong rung, capture provenance, Critic and Builder role blur, stack ceiling, punch list too shallow, and so on).
   - Evidence: cited stills, SxS pairs, and verdict lines.
   - What to stop doing: repeated dead-end tactics.
   - Steer instructions: numbered, concrete Builder actions for the next 1 to 2 rounds (may include an escalation rung). These may differ from the last Critic punch list, but they must still aim at the same Visual Bar and locked refs.
   - Success check: what should change in the next SxS blur pairs if the steer worked.
4. Resume the Builder with the Diagnoser steer brief, with the latest Critic punch list as secondary input. Log `DIAGNOSE steer applied`. Then return to Stage D capture and Critic.
5. If another 3 consecutive FAILs happen after a steer, diagnose again. Do not lower the bar. Do not soft-WIN.

The Diagnoser does not replace the Critic. Only a fresh Critic can return `VERDICT: WIN`. The Diagnoser never edits the game.

## Stage D: Capture and Critic loop (visual parity)

Entry: Stage C exit.

### Capture protocol

The Builder captures with scripts committed under `tools/`:

1. Stills: one per locked ref (so 4 to 8), framed to match that ref's slot, camera distance, and subject. Native render resolution, minimum 1920x1080, PNG. Save as `artifacts/stills/still-NN.png`.
2. `artifacts/stills/MANIFEST.md`: for each still, the file, build commit, resolution, scene, camera, timestamp, and matched ref.
3. Walkthrough: 45 to 90 seconds, 720p minimum (prefer 1080p), from the live build. Cover boot or title, the core loop, a win or fail state, and HUD states. Save as `artifacts/walkthrough.mp4`.
4. Walkthrough frames: extract 8 to 12 evenly spaced frames to `artifacts/walkthrough-frames/` so a Critic that cannot play video can still score C5 (walkthrough consistency).
5. SxS: `tools/make-sxs` builds `artifacts/sxs-vs-refs/sxs-NN.png` (left = ref labeled `REF (review only)`, right = game capture labeled `GAME`, matched height) and `sxs-NN-blur.png` (both halves blurred with the same radius, then downscaled).
6. Handback in the fixed format. No quality claims.

### Critic loop

The Orchestrator runs:

1. Check the capture spec (counts, resolution, manifest, video length). If anything is short, send it back for capture without spawning the Critic.
2. Spawn a fresh Critic with `critic-prompt.md`, `BRIEF.md`, `art/LOOK.md`, `art/BAR.md`, `refs-locked/`, the capture set, and the previous verdict file.
3. Validate the verdict: exact format, every criterion in `art/BAR.md`, every still cited, no banned phrases, no numeric scores. Reject and re-run the Critic if it is invalid or if it scored a different set than `art/BAR.md`.
4. `RECAPTURE`: fix the capture defects, re-capture, re-score. Not a round.
5. `FAIL`: log `R<n> FAIL` and save the verdict to `artifacts/verdicts/R<nn>.md`. If this is the 3rd consecutive hard FAIL (or another stuck-loop trigger fired), do not dispatch another punch yet: run stuck-loop diagnosis, then dispatch the Builder with the Diagnoser steer brief. Otherwise dispatch the Builder with the punch list verbatim. The Builder fixes, re-captures every still and the walkthrough (fixes change every frame, so partial re-captures are rejected), and hands back. Return to step 1 as R<n+1>.
6. `WIN` (Critic stamp only): do not hand off yet.
   1. Run the pre-handoff verification below. If it fails, fix the process issue and re-run the Critic.
   2. Run the Orchestrator harsh visual (required). Open every current still and every SxS (with blur variants) at full resolution yourself. Write a short plain read of what the picture actually shows: art-system unity; proportion and scale consistency across characters, props, buildings, and animals; perspective and ground-plane logic; lighting and shadow coherence; and whether the game half belongs next to the locked refs. The user's visual judgment overrides Critic WIN for shippability. Incoherent art, off proportions, or broken perspective is a FAIL even if the Critic stamped WIN.
   3. If the harsh visual FAILs: log `WIN voided` (or `ORCH-FAIL`) in `artifacts/rounds.log`, tell the user once with the stills and the plain read, treat the Critic WIN as void, keep Publish LOCKED, and resume Stage D punches (or stuck-loop diagnosis if the voided WIN sits on a FAIL streak). Do not soft-WIN. Do not hand off.
   4. If the harsh visual PASSes and pre-handoff is clean: log `WIN` and hand off. Post the stills and the plain read to the user.
7. Post one status line to the user after every round, in the format given in `SKILL.md`. Do not ask permission to continue. On a diagnosis hold, the status line's `next` is `Diagnoser`, then `Builder steered R<n+1>`.

Exit: a valid Critic `WIN`, plus pre-handoff verification passed, plus Orchestrator harsh visual PASS.

## Stage E: Comparison matrix (optional)

Run only when the user or the host launch config names the legs. Never invent legs. A leg is usually one model under test or one stack. Label legs neutrally (model A, model B, or stack A, stack B), never with model identifiers.

- Same `BRIEF.md` and `refs-locked/` for every leg. One Builder per leg in its own workdir. Each leg runs its own Stage D loop with its own `rounds.log` and verdicts.
- Prefer proving Stage B on one leg before going parallel, unless the user wants all legs live.
- Assemble the matrix SxS (ref, leg A, leg B, ...) only from legs with WIN, or best-so-far if the user accepts that explicitly. Label best-so-far legs `FAIL` in the composite.
- A leg is never WIN because it beats another leg. The bar is the refs.

## Critic contract

The full drop-in prompt, including the exact output block for every verdict, is `critic-prompt.md`. What the Orchestrator needs in order to dispatch and validate:

Inputs (files only): `BRIEF.md`, `art/LOOK.md`, `art/BAR.md`, `refs-locked/*` with `SOURCES.md`, `artifacts/stills/*` with `MANIFEST.md`, `artifacts/sxs-vs-refs/*`, `artifacts/walkthrough.mp4` or `artifacts/walkthrough-frames/*`, the previous verdict file, scope, round, and commit.

Procedure: inventory inputs (RECAPTURE if the set is under spec), inspect every still at full resolution against its matched ref, run the blur test on every pair, sample walkthrough frames, mark each criterion PASS or FAIL with cited evidence, and write the verdict in the exact format.

Verdict vocabulary (accept nothing else):

- `VERDICT: WIN` (Stage D only)
- `VERDICT: FAIL` with `PUNCH LIST` (any scope)
- `VERDICT: RECAPTURE` with a numbered list (capture defects, not a round)
- `VERDICT: ART-PASS C<n>` (Stage C sub-gate)
- `VERDICT: B-PASS (loop only, NOT A VISUAL WIN)` (Stage B)
- `VERDICT: PLAN-OK` or `VERDICT: PLAN-GAPS` (Stage A plan review)
- `VERDICT: BLOCKED` with a reason (the Critic cannot view images, or `art/BAR.md` is outside 5 to 15 criteria)

Punch item schema: `N. [criterion] still-id vs ref-id, region: observed defect; what the ref shows. Done when <observable condition>.` Order by impact on the blur test. Group repeats into one item listing all still ids. No implementation prescriptions.

Forbidden in verdicts: banned phrases, numeric scores, partial credit, "PASS with notes", scoring a subset of stills, crediting improvement over the previous round, commentary on fun or gameplay, suggestions to change refs or relax the bar, model identifiers.

Rounds: several rounds after a strong Stage C is normal, and ten or more is normal when the gap is large. The Critic re-checks every criterion in `art/BAR.md` every round, including those that passed before.

## Deliverables

```
BRIEF.md
PLAN.md
README.md                      run instructions, publish status, no model identifiers
refs-locked/
  ref-01-<slug>.png ... ref-NN-<slug>.png
  SOURCES.md
art/
  LOOK.md
  BAR.md                       locked Visual Bar for this game (core + game-specific)
  LEDGER.md
  GENERATION-LOG.md
artifacts/
  stage-b/boot.log, loop-frames/     Stage B evidence
  stage-c/C1/ ... C5/                interim captures per art sub-gate
  stills/still-01.png ... MANIFEST.md
  walkthrough.mp4
  walkthrough-frames/
  sxs-vs-refs/sxs-01.png, sxs-01-blur.png ...
  verdicts/A-plan.md, B.md, C1.md ... C5.md, R01.md ...
  diagnosis/R<n>-steer.md            stuck-loop Diagnoser briefs (when triggered)
  rounds.log
tools/
  capture.*                    stills and video from the live build
  make-sxs.*                   composites and blur variants
```

`artifacts/rounds.log` has one line per event:

```
<ISO time> | stage=<A|B|C0..C5|D|E-<leg>> | round=<R<n>|n/a> | role=<planner|builder|critic|diagnoser|orchestrator> | session=<id|n/a> | request=<id|n/a> | commit=<sha> | result=<PASS|FAIL|WIN|WIN voided|ORCH-FAIL|RECAPTURE|ESCALATE rung <n>|DIAGNOSE hold|DIAGNOSE steer applied|REF-SWAP|BAR-EXPAND|PUBLISH-UNLOCK> | note=<short>
```

`session` is the host's identifier for the subagent session and `request` is the host's identifier for the dispatch call. Identifiers only: no keys, tokens, or credential-bearing URLs.

## Pre-handoff verification

- [ ] The WIN verdict is valid (format, every `art/BAR.md` criterion PASS, all stills cited, no banned phrases, no scores)
- [ ] Orchestrator harsh visual PASS on the current stills and SxS (cohesion, proportions, perspective, light). Critic WIN alone does not clear this, and the user's visual judgment overrides Critic WIN for shippability
- [ ] `art/LEDGER.md` has zero `placeholder` rows
- [ ] `MANIFEST.md` commits match the delivered build commit
- [ ] No ref image bytes inside the game's asset or build directories (hash compare against `refs-locked/`)
- [ ] All text artifacts scanned for vendor names, model family names, and version-suffixed model identifiers. Any hit blocks handoff
- [ ] All text artifacts and logs scanned for credential patterns (key, token, secret, password assignments, bearer values). Any hit blocks handoff
- [ ] `README.md` has boot instructions verified exit 0 and states `Publish: LOCKED` (or the logged unlock)
- [ ] Original names only in title, UI, and assets

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Critic passes in R8 what it failed in R3 with no visible change | Rubric drift from long context | Fresh Critic every round, file inputs only, banned phrase validation. The Orchestrator rejects the verdict |
| Builder handback says "looks great, should pass" | Role blur | Fixed handback format, no verdict words. The Orchestrator forwards only the capture set |
| Art landed once after placeholders and still reads flat | Skipped the C1 lighting rig, no ledger | Run C0 to C5 in order, ART-PASS per sub-gate, ledger all final before D |
| Stills look right, walkthrough shows popping or magenta | Cherry-picked stills | Core C5, frame extraction, Critic samples frames |
| Same criterion fails two checks running with no visible change in the SxS | Stack ceiling, escalation trigger ignored | Escalate one rung now, log `ESCALATE rung <n>`, re-run affected C gates if rung 4 or 5 |
| Refs quietly replaced with weaker captures | Gaming the bar | `REF-SWAP` logged with a reason, never to a weaker ref, Critic re-scores from scratch |
| Vendor or model names in README or plan | Copied from host config | Pre-handoff scan blocks handoff |
| Generated output resembles a franchise character or logo | Prompt leakage or ref used as input | Regenerate with original descriptors, note it in the ledger, never use refs as image input |
| "Deployed a preview so the critic could look" | Convenience publish | The Critic scores local captures. Publish stays LOCKED |
| Critic scores from a text description of the frames | No image capability | Report BLOCKED. Do not run the loop without an image-capable Critic |
| Loop stops after one polite attempt | Treating FAIL as an ending | FAIL dispatches the next round unless stuck-loop diagnosis triggers. Stop only on WIN, user stop, or hard blocker |
| Same punch family for 3+ FAILs with no SxS progress | Thrashing the same tactics, wrong rung | Pause punches, spawn a Diagnoser, apply the steer brief, escalate a rung if stack-limited. Do not soft-WIN |
| Diagnoser rewrites the bar or soft-passes | Role blur | Reject the diagnosis. The Diagnoser may change tactics, never the Visual Bar or refs |
| Critic stamped WIN but stills look like a kit collage, toy scale, or broken perspective | Orchestrator skipped the harsh visual; rubric-only WIN | Void the WIN, log `WIN voided`, resume punches. Critic WIN is necessary, not sufficient |
| Same fixed criteria on a cockpit game and a MOBA | Static bar | Lock `art/BAR.md` at Stage A from the refs (5 to 15 criteria), add game-specific criteria, never score a generic list when `BAR.md` differs |

## Handoff template

```
## Game builder handoff
Project: <original title> (<stack>)
Stage reached: <A|B|C0..C5|D|E>
Verdict: WIN | FAIL (best so far) | STOPPED BY USER | BLOCKED (<reason>)
Rounds: R<n> in Stage D. Escalations: <rungs used or none>. Diagnoses: <count and paths, or none>
Visual parity vs refs: met | short on `art/BAR.md` criteria <list>
Open punch list: <verbatim from last verdict, or none>
Paths:
  BRIEF.md, PLAN.md, README.md
  refs-locked/ (<N> refs, SOURCES.md)
  artifacts/stills/ (<N>, MANIFEST.md)
  artifacts/walkthrough.mp4 (<length>), artifacts/walkthrough-frames/
  artifacts/sxs-vs-refs/ (<N> pairs plus blur variants)
  artifacts/verdicts/, artifacts/rounds.log
Boot: <command>, exit 0 verified at <commit>
Publish: LOCKED (awaiting user unlock) | UNLOCKED by user at <time>
IP: original names and art. Refs used for review only, not embedded in the build.
Identifiers: session and request ids in artifacts/rounds.log. No secrets.
Model identifiers in artifacts: none (scan clean)
```

If the user stops mid-loop, report best-so-far honestly and state whether visual parity is still short of the refs, and on which criteria.
