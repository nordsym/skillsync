# Examples: good vs bad

Short paired examples for each role. Every "bad" example is something a real run produces or would accept. Every "good" example is the same situation handled under the skill's rules. No AI vendor or model names appear anywhere in artifacts, including these.

All examples assume this `art/BAR.md` (six criteria): C1 Materials and lighting, C2 Geometry density, C3 SxS blur test, C4 Live capture provenance, C5 Walkthrough consistency (the universal core), plus C6 HUD craft (game-specific, justified by HUD-heavy refs).

## Contents

1. Critic verdict on a flat-looking game
2. Punch item wording
3. Using RECAPTURE
4. Critic behavior across rounds
5. Builder handback
6. Orchestrator status line
7. Escalation
8. Refs
9. A valid WIN

## 1. Critic verdict on a flat-looking game

Situation: R4. Stills show a textured hero unit on a single-color green ground plane, no contact shadows, HUD in a default sans-serif over a translucent black strip. Gameplay loop works.

Bad (soft WIN, invalid):

```
Verdict: WIN (with reservations)
The game has come a long way since R1 and the loop is genuinely fun. Ground is still
a bit flat and the HUD could use more chrome, but for a browser homage that is not
trying to be photoreal this is a solid slice. Score 7/10. Ship it.
```

Why it fails validation: banned phrases ("with reservations", "browser homage", "not trying to be photoreal", "solid slice"), a numeric score, credit for improvement, gameplay commentary, no still ids cited, wrong format.

Good (FAIL, valid):

```
VERDICT: FAIL
SCOPE: D
ROUND: R4
CAPTURE: 3f9c2a1
BAR: art/BAR.md (6 criteria)
CRITERIA: C1 FAIL | C2 FAIL | C3 FAIL | C4 PASS | C5 PASS | C6 FAIL
PUNCH LIST:
1. [C2 Geometry density] still-01, still-02, still-04 vs ref-01, ref-02, ref-04, lower half of frame: ground is one flat green plane with no rocks, debris, decals, or vegetation; refs show broken ground with varied silhouettes at this camera distance. Done when matched crops show a comparable count and variety of ground silhouettes and the blur pairs match in density.
2. [C1 Materials and lighting] all stills, unit and prop bases: no contact darkening where objects meet the ground; refs show ambient occlusion at every base and crevice. Done when every still shows contact shadows under units and props.
3. [C1 Materials and lighting] still-03 vs ref-03, ground and walls: single-color surfaces with no roughness or normal detail; ref shows worn stone with specular breakup. Done when no large surface in any still reads as a single flat color.
4. [C6 HUD craft] all stills, HUD strip: default sans-serif typeface over a translucent black bar; ref HUD uses a weighted display typeface, framed panels, and an icon set. Done when the HUD reads as game UI in the ref grammar at 1080p and at half scale with no default fonts.
5. [C3 SxS blur test] sxs-01-blur through sxs-04-blur: game half reads flatter and emptier with a higher-contrast primary green; refs read as dense mid-value scenes. Done when every blurred pair reads as the same production tier.
```

## 2. Punch item wording

Bad: `Ground looks kind of empty. Consider adding SSAO and switching to a deferred renderer.`

Why: vague region, no still or ref ids, no done-when condition, prescribes implementation.

Good: `1. [C2 Geometry density] still-02 vs ref-02, lower third: flat plane between the two towers with visible tile seams; ref shows rubble, grass clumps, and decals. Done when the same region reads as varied surface at this camera distance and sxs-02-blur matches in density.`

## 3. Using RECAPTURE

Bad (RECAPTURE used to dodge a FAIL):

```
VERDICT: RECAPTURE
1. Please capture from a more flattering angle so the ground reads better.
```

Why: framing is set by the refs, not by what flatters the game. The ground defect is a C2 FAIL and belongs in a punch list.

Good:

```
VERDICT: RECAPTURE
1. still-02 is framed at roughly twice the camera distance of ref-02; re-capture at matched distance and subject.
2. MANIFEST.md lists commit 3f9c2a1 for still-05 but the delivered build is 41d0e77.
3. sxs-03-blur.png missing.
```

## 4. Critic behavior across rounds

Bad: in R3 the Critic fails C6 HUD craft for default fonts. In R8, with the same HUD typeface still in place, the Critic writes `C6 HUD craft: PASS. Consistent with earlier rounds and much improved overall.`

Why: rubric drift. Nothing in the HUD changed, and "improved" is not a criterion. This is exactly why the Critic is spawned fresh each round with the previous verdict as a file and no chat history. The Orchestrator rejects this verdict.

Good: the R8 Critic opens still-01 through still-06 beside the refs, notes the same default typeface, and writes the C6 punch item again, grouped across all stills.

## 5. Builder handback

Bad:

```
Done with R5. Ground looks way better now, added tons of rocks and the lighting is
basically parity. Should be a WIN this time. Didn't get to the HUD font but it's minor.
```

Why: quality claims, verdict words, "minor" is a judgment the Builder does not own, no commit or capture paths.

Good:

```
ROUND R5 COMPLETE
commit: 41d0e77
captures: artifacts/stills/ (6), artifacts/walkthrough.mp4, artifacts/walkthrough-frames/ (10), artifacts/sxs-vs-refs/ (6 pairs)
addressed: 1, 2, 3, 5
not addressed: 4 (typeface licensing for redistribution still unresolved; two candidate original typefaces in art/LEDGER.md)
```

## 6. Orchestrator status line

Bad: `R5 looks great, I think we're basically there. Want me to keep going or call it?`

Why: the Orchestrator does not score, does not use soft language, and does not ask permission to continue.

Good: `R5 FAIL: failing criteria C3, C6; punch items 2; escalation rung none; next: Builder R6`

## 7. Escalation

Bad: after C2 fails in R2, R3, and R4 with the same flat ground in every SxS, the Builder keeps adding a few more prop instances per round and the Orchestrator keeps dispatching the same rung.

Why: the trigger fired at R3 (same criterion, two consecutive checks, no visible change in the matched SxS). The Orchestrator should have logged `ESCALATE rung 3` (density: instanced scatter, terrain displacement, debris) and, if that also stalled, rung 4 (renderer features).

Good log lines:

```
2026-01-14T18:02:11Z | stage=D | round=R3 | role=orchestrator | session=n/a | request=n/a | commit=9be1c04 | result=ESCALATE rung 3 | note=C2 unchanged in sxs-01..04 across R2,R3; moving to instanced scatter and terrain displacement
2026-01-14T21:40:52Z | stage=D | round=R6 | role=orchestrator | session=n/a | request=n/a | commit=c77a2d3 | result=ESCALATE rung 4 | note=C1 contact darkening unreachable on current pipeline; moving to a pipeline with screen-space AO and a full post stack; re-running C1..C5 gates
```

## 8. Refs

Bad: at R6 the Orchestrator replaces two high-density refs with lower-resolution captures from a lighter title "to keep the comparison fair to a browser build."

Why: swapping to a weaker ref lowers the bar. The bar is the refs' tier. If the stack cannot reach it, escalate the stack.

Good: refs stay locked. If a ref must change (for example, a source URL turned out not to be a shipped-game capture), the Orchestrator logs `REF-SWAP` with the reason, swaps to a ref of equal or higher tier, and the Critic re-scores from scratch.

## 9. A valid WIN

```
VERDICT: WIN
ROUND: R11
CAPTURE: e4a91f0
BAR: art/BAR.md (6 criteria)
CRITERIA:
C1 Materials and lighting: PASS. still-01..06: roughness and normal breakup on stone, metal, and cloth; key light from upper left with cast shadows on ground and props in every still; contact darkening at all bases; grade inside ref palette; fog and bloom consistent with ref-04 mood.
C2 Geometry density: PASS. still-01..06 matched crops: ground broken by rubble, grass, decals, and displacement; units and props read as multi-part silhouettes; silhouette count and variety in the same range as ref-01..06. No primitives in frame.
C3 SxS blur test: PASS. sxs-01-blur..sxs-06-blur: both halves read as the same genre and tier in value structure, palette, density, and light direction.
C4 Live capture provenance: PASS. MANIFEST.md lists e4a91f0 for all six stills at 2560x1440; crop and labels only.
C5 Walkthrough consistency: PASS. frames 01..10: no placeholder, no missing texture, no flicker or popping; title, loop, fail state, and all HUD states shown.
C6 HUD craft: PASS. still-01..06: weighted display typeface with three-level hierarchy, framed panels with plate and border treatment, one icon set, text on plates; readable at 1080p and at half scale.
```

Note what is absent: no score, no praise, no comparison to earlier rounds, no comment on gameplay. Every criterion cites every still.

A Critic WIN is still not a shippable WIN. The Orchestrator now runs pre-handoff verification and its own harsh visual read of the same stills and SxS before handing off.
