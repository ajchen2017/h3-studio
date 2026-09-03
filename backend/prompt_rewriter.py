"""Builds MiniMax H3's official ref2va prompt format from h3-studio's own
draft (subject_definitions:/summary:/detailed_description:/
overall_soundscape:/non_diegetic_music:, possibly Chinese, missing
retention_analysis, wrong shot-timestamp format).

2026-08-24 history: the first version of this module did the whole
translate+restructure+add-retention_analysis job in a single big Haiku
call. That worked most of the time but Haiku occasionally ignored the
actual input and invented a completely unrelated scene instead (a mountain
warrior, then separately an ancient-temple treasure box - both replacing a
literal breakfast-shop-with-buns input, in both cases while status quo
GPU time was already burning on the wrong prompt). This SDK
(anthropic==1.0.0) has no temperature/top_p knob on messages.create() to
dial down that risk directly, and strengthening the "don't invent content"
instruction reduced but did not eliminate it - confirmed by it recurring
after that fix, specifically when a real <Subject N> reference was
present (compare: 4/4 clean on prompts with subject_definitions left
blank, 1st possible retest with a real Picture 1 reference re-hallucinated).

Root cause read: "translate this AND restructure it into a specific
narrative format" is inherently more of a creative-writing task than
"translate this text" is - creative-writing tasks are exactly where an
LLM's own priors (iconic "discovery" scenes, etc.) compete with the
actual input for what gets generated. The fix is to shrink what's
delegated to the LLM's judgment down to a narrow, low-risk translation
step, and do everything else (retention_analysis, section labels, task
prefix) as plain deterministic Python that literally cannot hallucinate.
Shot timestamps are evenly split across however many [Shot N] markers
exist for now (good enough until the step-3 UI redesign gives real
per-shot timing to work from).
"""

import json
import os
import re

import anthropic

MODEL = "claude-haiku-4-5-20251001"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _has_non_ascii(text: str) -> bool:
    return any(ord(c) > 127 for c in text)


# Dialogue/lyrics content inside <d>[Language] ...</d> must stay in its
# original language verbatim per the official guide - only the [Language]
# tag says what language it's in, the content itself is never translated.
_DIALOGUE_PATTERN = re.compile(r"<d>\s*\[[^\]]+\]\s*.*?</d>", re.DOTALL)


def _translate(text: str) -> str:
    """Narrow, faithful English translation - not a restructuring task, so
    far lower hallucination risk than the old combined approach. Skips the
    API call entirely for already-English text.

    2026-08-25 fix: dialogue inside <d>[Chinese] ...</d> was getting
    translated to English right along with everything else - the old
    system prompt told Haiku to preserve "<d>...</d>...just in English",
    which it read literally as "translate the dialogue text too". That's
    backwards: the [Language] tag says what language the quoted words
    already are, and the guide requires keeping them in that language,
    untouched. Rather than trust Haiku to selectively skip only the
    dialogue while translating everything around it (exactly the kind of
    judgment call that caused the hallucination incidents this module's
    architecture was redesigned to avoid), dialogue blocks are pulled out
    with a plain regex before the API call and spliced back in verbatim
    after - Haiku never sees them at all, so there's nothing for it to
    mistranslate.
    """
    text = text.strip()
    if not text or not _has_non_ascii(text):
        return text

    placeholders = []

    def _stash(m):
        placeholders.append(m.group(0))
        return f"XH3DIALOGUEX{len(placeholders) - 1}X"

    stashed = _DIALOGUE_PATTERN.sub(_stash, text)

    if not _has_non_ascii(stashed):
        # Nothing left to translate once the dialogue (the only non-ASCII
        # part) was pulled out.
        result = stashed
    else:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=1000,
            system=(
                "Translate the given text to English. This is a literal translation task, not a "
                "rewrite - preserve every concrete detail (objects, actions, numbers, names, "
                "structural markers like [Shot 1], <Subject 1>, (S1), and tokens matching "
                "XH3DIALOGUEXnX) exactly as they appear, just in English. Tokens matching "
                "XH3DIALOGUEXnX (n is a number) are opaque placeholders - copy them through "
                "completely unchanged, character for character, never translate or alter them. "
                "Do not add, remove, or embellish content. "
                "Respond with ONLY the translated text, nothing else."
            ),
            messages=[{"role": "user", "content": stashed}],
        )
        result = resp.content[0].text.strip()
        # Safety net for the 2026-08-24 incident: a curl/argv encoding bug (not
        # Haiku) once sent genuinely corrupted bytes here, and instead of
        # erroring, Haiku replied with an apologetic "I can't read this text"
        # disclaimer - which silently got used as if it were a real
        # translation. Refuse to pass a refusal off as a translation.
        if len(result) > 40 and re.search(r"\b(I'm unable to|I cannot|I can't|appears to be corrupted|doesn't correspond to)\b", result, re.IGNORECASE):
            raise RuntimeError(f"Haiku 回覆看起來像是拒絕翻譯，不是真正的翻譯結果（可能是輸入編碼壞掉）：{result[:200]}")

    for i, original in enumerate(placeholders):
        result = result.replace(f"XH3DIALOGUEX{i}X", original)
    return result


_SECTION_LABELS = [
    "subject_definitions",
    "summary",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
]


def _parse_sections(raw_prompt_text: str) -> dict:
    """Splits h3-studio's own draft format back into its labeled parts.
    Tolerant of missing sections (returns '' for those) since this also
    has to handle hand-typed or slightly malformed input."""
    pattern = r"(?im)^(" + "|".join(_SECTION_LABELS) + r"):\s*"
    parts = re.split(pattern, raw_prompt_text)
    # re.split with a capturing group yields [pre, label1, body1, label2, body2, ...]
    sections = {label: "" for label in _SECTION_LABELS}
    for i in range(1, len(parts) - 1, 2):
        label = parts[i].lower()
        body = parts[i + 1].strip()
        if label in sections:
            sections[label] = body
    return sections


def _fix_shot_timestamps(detailed_description: str, duration_seconds: float) -> str:
    """Deterministic (not LLM). The step-3 UI (2026-08-24) lets users insert
    a [Shot N] marker with an explicit, correct 'At MM:SS.mmm,' timestamp
    already attached - those are left untouched. Only bare `[Shot N]`
    markers with no timestamp (hand-typed drafts, older projects) fall back
    to splitting the segment duration evenly across however many shots are
    present, same as before this UI existed."""
    shot_pattern = re.compile(r"\[Shot\s*(\d+)\]\s*(?:At\s+([\d:.]+),\s*)?")
    matches = list(shot_pattern.finditer(detailed_description))
    if not matches:
        return detailed_description

    n = len(matches)
    per_shot = duration_seconds / n
    out = []
    last_end = 0
    for i, m in enumerate(matches):
        out.append(detailed_description[last_end:m.start()])
        shot_num = m.group(1)
        explicit_ts = m.group(2)
        if i == 0:
            out.append(f"[Shot {shot_num}] ")
        elif explicit_ts:
            out.append(f"[Shot {shot_num}] At {explicit_ts}, ")
        else:
            start_sec = per_shot * i
            minutes = int(start_sec // 60)
            seconds = start_sec % 60
            out.append(f"[Shot {shot_num}] At {minutes:02d}:{seconds:06.3f}, ")
        last_end = m.end()
    out.append(detailed_description[last_end:])
    return "".join(out)


# Official retention_analysis relationship markers (VIDEO_PROMPT_WRITING_GUIDE_ref_en.md),
# strongest identity preservation first. The trailing clause paraphrases
# each marker's official definition.
RETENTION_LEVELS = {
    "fully_preserved": "the face and identity are retained; other elements (setting, props, wardrobe) are newly generated per the detailed_description.",
    # 2026-08-26: for a continuous multi-segment take (e.g. one person
    # talking to camera for several minutes, split across segments purely
    # because of the 15s-per-clip cap) - every other level explicitly hands
    # wardrobe/setting/props to detailed_description, which drifts every
    # segment unless the user hand-repeats identical scene text each time.
    # This one instead locks everything the reference image shows, so the
    # detailed_description only needs to describe pose/action/camera.
    "fully_preserved_with_scene": "the face, identity, wardrobe, and setting are all retained exactly as shown in the reference image; only pose, action, and camera framing are newly generated per the detailed_description.",
    # 2026-08-26: for a background-removed/cutout reference photo - there's
    # no real setting in the image to lock onto, so telling the model
    # setting is "retained exactly as shown in the reference" (the level
    # above) has nothing to point at and just conflicts with whatever
    # scene detailed_description actually describes. This locks face+
    # wardrobe only, same as the level above minus setting.
    "fully_preserved_with_wardrobe": "the face, identity, and wardrobe are retained exactly as shown in the reference image; the setting is newly generated per the detailed_description.",
    "partially_preserved": "some defined characteristics (e.g. facial features) are retained while others may change; other elements are newly generated per the detailed_description.",
    "attribute_transfer": "characteristics from the reference are transferred onto a different identifiable subject in the scene; other elements are newly generated per the detailed_description.",
    "weak_reference": "only broad style, category, composition, or atmosphere similarity is retained, not strict identity; other elements are newly generated per the detailed_description.",
}


def _build_retention_analysis(
    ref_image_roles: list,
    detailed_description: str,
    retention_level: str = "fully_preserved",
    has_ref_audio: bool = False,
) -> str:
    """ref_image_roles: one dict per reference image slot, in ref_image_0/1/2
    order (so index+1 == its <Picture N> number regardless of role):
    {"role": "subject"} (default - identity reference, becomes <Subject N>)
    or {"role": "anchor", "shot": int, "frame_type": "first"|"keyframe"|"last"}
    (a compositional frame anchor per guide section 2.2/5.3 - becomes a
    standalone <Picture N> entry instead, no <Subject N> assigned)."""
    if not ref_image_roles and not has_ref_audio:
        return ""
    if retention_level not in RETENTION_LEVELS:
        retention_level = "fully_preserved"
    shot_numbers = sorted(set(re.findall(r"\[Shot\s*(\d+)\]", detailed_description)), key=int)
    shots_list = ", ".join(f"[Shot {n}]" for n in shot_numbers) if shot_numbers else "[Shot 1]"
    frame_labels = {"first": "first frame", "keyframe": "keyframe", "last": "last frame"}
    lines = []
    subject_counter = 0
    for picture_n, role_info in enumerate(ref_image_roles, start=1):
        role_info = role_info or {}
        if role_info.get("role") == "anchor":
            shot = role_info.get("shot") or 1
            frame_label = frame_labels.get(role_info.get("frame_type"), "first frame")
            lines.append(
                f"<Picture {picture_n}> ([Shot {shot}] {frame_label}): {retention_level} - {RETENTION_LEVELS[retention_level]}"
            )
        else:
            subject_counter += 1
            lines.append(
                f"<Subject {subject_counter}> (appears in {shots_list}): {retention_level} - {RETENTION_LEVELS[retention_level]}"
            )
    if has_ref_audio:
        lines.append(
            "<Audio 1>: reference - the target speaker follows <Audio 1>'s voice timbre and delivery without copying the original signal."
        )
    return "\n".join(lines)


def rewrite_prompt(
    raw_prompt_text: str,
    ref_image_roles: list,
    duration_seconds: float,
    retention_level: str = "fully_preserved",
    has_ref_audio: bool = False,
) -> str:
    sections = _parse_sections(raw_prompt_text)

    subject_definitions = _translate(sections["subject_definitions"])
    summary = _translate(sections["summary"])
    detailed_description = _translate(sections["detailed_description"])
    overall_soundscape = _translate(sections["overall_soundscape"])
    non_diegetic_music = _translate(sections["non_diegetic_music"]) or "N/A"

    detailed_description = _fix_shot_timestamps(detailed_description, duration_seconds)
    retention_analysis = _build_retention_analysis(ref_image_roles, detailed_description, retention_level, has_ref_audio)

    # Task-type prefix per guide section 3 - combine keyframe completion
    # (anchor-role images) and reference generation (subject-role images)
    # rather than always hard-coding "[reference generation]" regardless
    # of what's actually attached.
    task_types = []
    if any((r or {}).get("role") == "anchor" for r in ref_image_roles):
        task_types.append("keyframe completion")
    if any((r or {}).get("role", "subject") == "subject" for r in ref_image_roles):
        task_types.append("reference generation")
    if task_types and not summary.startswith("["):
        prefix = f"[{' + '.join(task_types)}]"
        summary = f"{prefix} {summary}" if summary else prefix

    return (
        f"subject_definitions:\n{subject_definitions}\n\n"
        f"summary: {summary}\n\n"
        f"retention_analysis:\n{retention_analysis}\n\n"
        f"detailed_description:\n{detailed_description}\n\n"
        f"overall_soundscape: {overall_soundscape}\n\n"
        f"non_diegetic_music: {non_diegetic_music}"
    )


# ---------------------------------------------------------------------------
# AI-assisted draft generation ("✨ AI 產生 Prompt", 2026-08-25)
#
# Deliberately NOT built on _translate()'s narrow-task philosophy - this one
# call is supposed to be creative (expand one sentence into a shot-by-shot
# scene), unlike everything else in this module. The scope is kept as small
# as the task allows to limit surface area for the same kind of drift that
# caused the 2026-08-24 hallucination incidents:
#   - No timestamps - bare [Shot N] markers only, _fix_shot_timestamps()
#     (deterministic) fills in "At MM:SS.mmm," afterward.
#   - No subject_definitions/retention_analysis - those already come from
#     the user's actual ref-image/audio selections, a free-text idea has no
#     business inventing or restating them.
#   - Output is inserted into the editable Shot/soundscape fields, not
#     submitted directly - the user reviews and can hand-edit before this
#     goes anywhere near prompt_rewriter.rewrite_prompt() or ComfyUI.
# ---------------------------------------------------------------------------

_SUMMARY_PATTERN = re.compile(r"SUMMARY:\s*(.*?)\s*(?=SEGMENT\s*\d+:)", re.DOTALL)
_SEGMENT_DRAFT_PATTERN = re.compile(
    r"SEGMENT\s*(\d+):\s*SHOTS:\s*(.*?)\s*SOUNDSCAPE:\s*(.*?)(?=\s*SEGMENT\s*\d+:|\Z)", re.DOTALL
)

# 2026-08-25 stress test (12-segment single-call generation): Haiku reliably
# used the "(S1) says in an off-screen voiceover: <d>...</d>" template but
# dropped the mandatory trailing ", lips remain completely closed." clause
# in 3/3 voiceover lines, even though the template in the prompt spells it
# out verbatim. Same principle as the rest of this module - fix
# deterministically in Python instead of trying to word the prompt into
# 100% compliance.
_VOICEOVER_MISSING_LIPS_PATTERN = re.compile(
    r"(off-screen voiceover:\s*<d>\[[^\]]*\].*?</d>)(?!\s*,\s*lips remain completely closed)",
    re.DOTALL,
)


def _ensure_voiceover_lips_closed(text: str) -> str:
    return _VOICEOVER_MISSING_LIPS_PATTERN.sub(r"\1, lips remain completely closed.", text)


def generate_shot_draft(
    idea: str, duration_seconds: float, num_subjects: int, has_audio: bool, num_segments: int = 1
) -> dict:
    """Expands a short free-text idea into a one-sentence Chinese summary
    plus a multi-shot draft, one {"shot": str, "soundscape": str} entry per
    segment, matching the structural conventions the rest of the app
    already expects (Subject tags, <d>[Chinese]...</d> dialogue, no
    timestamps). Always uses the "SEGMENT N: ..." wrapper even for
    num_segments=1, so there's one parser instead of two near-duplicate
    code paths. Returns {"summary": str, "segments": [...]}."""
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("請先輸入一句話描述")
    num_segments = max(1, num_segments)

    approx_shots = 1 if duration_seconds <= 6 else (2 if duration_seconds <= 10 else 3)
    subject_note = (
        f"There are {num_subjects} reference character(s) available, tagged <Subject 1>"
        + (f" through <Subject {num_subjects}>" if num_subjects > 1 else "")
        + " - naturally involve them in the action using these exact tags. "
          "Do not invent additional named characters beyond these."
        if num_subjects > 0 else
        "No reference character is selected - describe the scene itself (environment, "
        "objects, action) without inventing a specific named person as the subject."
    )
    # 2026-08-25: used to hard-block dialogue entirely when has_audio was
    # False (no voice reference attached) - relaxed to match the same-day
    # generation-time relaxation (see index.html's submit-time <d> check):
    # a missing voice reference just means the model picks its own voice,
    # it's not a reason to forbid narration/dialogue outright. has_audio is
    # kept as a parameter (still computed by the caller) but no longer
    # gates whether dialogue is allowed, only in case future wording wants
    # to distinguish the two cases.
    #
    # 2026-08-25 stress test (12-segment full-article adaptation) found the
    # previous loose wording ("<d>[Chinese] ...</d> with a stable speaker id
    # right before it") was not followed reliably - Haiku produced
    # <d>(S1) ...</d> (id inside the tag, [Chinese] marker dropped entirely)
    # more than once, which silently defeats _translate()'s dialogue-stash
    # regex (it requires <d> immediately followed by [Language]) and lets
    # the dialogue get machine-translated to English right along with the
    # narrative text around it. Fixed by giving an exact fill-in-the-blank
    # template instead of a prose description, and by stating the SHOTS-only
    # placement rule explicitly (the same test also found dialogue leaking
    # into SOUNDSCAPE, which overall_soundscape must never contain).
    audio_note = (
        "Dialogue or off-screen voiceover narration is welcome if it fits naturally - do not force "
        "it in if the idea doesn't call for it. When you do use dialogue/narration, follow this "
        "structure and fill in only the Chinese text - do not deviate from the STRUCTURE, but the "
        "speech verb itself does not have to be literally \"says\":\n"
        "  - On-screen line (character shown speaking on camera): (S1) says: <d>[Chinese] 台詞內容</d>\n"
        "    - \"says\" may be replaced with a more expressive verb + manner clause when the moment "
        "calls for it, per the official guide's own example - e.g. (S1) exclaims with light "
        "annoyance, <d>[Chinese] 台詞內容</d> or (S1) whispers, <d>[Chinese] 台詞內容</d>. Plain "
        "\"says:\" is always a safe default when no particular tone is called for.\n"
        "  - Off-screen voiceover (speaker not shown talking): (S1) says in an off-screen voiceover: "
        "<d>[Chinese] 台詞內容</d>, lips remain completely closed.\n"
        "Rules: the speaker id like (S1) always goes BEFORE the <d> tag, never inside it. The literal "
        "[Chinese] tag must always immediately follow <d> - never write a bare <d> with no language tag. "
        "Reuse the same speaker id for the same speaker across every shot and segment it appears in, "
        "never renumber it - but EVERY distinct character who speaks gets their OWN id, assigned strictly "
        "in order of first appearance: the first character to speak anywhere in the story is (S1) for "
        "every line they ever speak, the second DIFFERENT character to speak (anywhere later) is (S2) "
        "for every line THEY speak, a third different character is (S3), and so on - this applies even "
        "if (S1) hasn't spoken again in a while, even in single-call multi-segment output. This is a "
        "common mistake, so check it explicitly before finishing: for EVERY <d> line you wrote, ask "
        "'is this the same physical person as the last time I used this id, or a different one?' - a "
        "worked example with two named characters, id assigned by first appearance and reused "
        "correctly, never merged:\n"
        "  SEGMENT 1: ...<Subject A> looks at <Subject B> and (S1) says: <d>[Chinese] 你今天怎麼這麼晚？</d>...\n"
        "  SEGMENT 2: ...<Subject B> laughs and (S2) says: <d>[Chinese] 路上塞車，別生氣嘛。</d>... "
        "(a NEW id, S2, because this is a different physical person than whoever S1 was, even though "
        "S1 hasn't spoken again yet)\n"
        "  SEGMENT 3: ...<Subject A> nods and (S1) says: <d>[Chinese] 好啦，快進來吧。</d>... "
        "(back to S1, because this is literally the same person as SEGMENT 1's speaker, not S2)\n"
        "Any dialogue/narration line belongs inside the SHOTS section only - never "
        "put a <d>...</d> line inside SOUNDSCAPE, which is for ambient/environmental sound only."
    )
    continuity_note = (
        f" Tell one continuous story across all {num_segments} segments - segment 2 picks up where "
        "segment 1 left off, and so on, rather than {num_segments} unrelated scenes."
        if num_segments > 1 else ""
    )

    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=400 * num_segments,
        system=(
            "You expand a short scene idea into a shot-by-shot video description in Traditional "
            "Chinese (繁體中文，台灣用語 - never Simplified Chinese characters), across {n} video "
            "segment(s), each roughly {duration:.0f} seconds long (~{shots} shot(s) "
            "per segment).{continuity} Stay closely tied to the given idea - do not invent unrelated "
            "characters, settings, or plot events beyond what it implies. {subject_note} {audio_note}\n\n"
            "Output EXACTLY this structure and nothing else - no extra commentary, no placeholder "
            "angle brackets, write plain Traditional Chinese sentences directly after each tag:\n"
            "SUMMARY:\n"
            "(one short Traditional Chinese sentence summarizing the whole video - what happens "
            "overall, not per-shot detail; do not add a task-type prefix like [reference generation], "
            "that gets added automatically elsewhere)\n\n"
            "SEGMENT 1:\n"
            "SHOTS:\n"
            "[Shot 1] (plain-language action + camera movement as one natural sentence, no angle brackets)\n"
            "[Shot 2] (more shots as appropriate for the duration - bare [Shot N] tags only, no time markers)\n"
            "SOUNDSCAPE:\n"
            "(one short Traditional Chinese line describing ambient/environmental sound for this segment)\n\n"
            "{more_segments}"
        ).format(
            n=num_segments, duration=duration_seconds, shots=approx_shots, continuity=continuity_note,
            subject_note=subject_note, audio_note=audio_note,
            more_segments=(
                "Repeat the exact same SEGMENT N: / SHOTS: / SOUNDSCAPE: structure for "
                f"SEGMENT 2 through SEGMENT {num_segments}."
                if num_segments > 1 else ""
            ),
        ),
        messages=[{"role": "user", "content": idea}],
    )
    result = resp.content[0].text.strip()
    summary_match = _SUMMARY_PATTERN.search(result)
    summary = summary_match.group(1).strip() if summary_match else ""
    matches = list(_SEGMENT_DRAFT_PATTERN.finditer(result))
    if len(matches) < num_segments:
        raise RuntimeError(f"Haiku 回覆格式不符預期，只解析到 {len(matches)}/{num_segments} 段：{result[:300]}")
    segments = []
    for m in matches[:num_segments]:
        shot_text, soundscape = m.group(2).strip(), m.group(3).strip()
        if not re.search(r"\[Shot\s*\d+\]", shot_text):
            raise RuntimeError(f"Haiku 回覆第 {m.group(1)} 段沒有包含任何 [Shot N] 標記：{shot_text[:300]}")
        shot_text = _ensure_voiceover_lips_closed(shot_text)
        segments.append({"shot": shot_text, "soundscape": soundscape})
    if not summary:
        raise RuntimeError(f"Haiku 回覆沒有包含 SUMMARY：{result[:300]}")
    return {"summary": summary, "segments": segments}


# ---------------------------------------------------------------------------
# Character extraction (2026-08-26) - feeds backend/character_workflow.py's
# SD3.5 portrait generator. Motivation: the "no reference image" path
# relies entirely on prose to keep a character's face consistent across
# independently-generated segments, and per the retention_analysis section
# of this module, prose alone doesn't lock identity the way a real
# <Picture N> reference does - this extracts who's actually in a piece of
# text so the user can generate and attach a real reference photo instead.
# ---------------------------------------------------------------------------

_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$")


def extract_characters(text: str) -> list[dict]:
    """Returns [{"name": str, "description_zh": str, "description_en": str}, ...].
    description_en is what actually drives the SD3.5 portrait prompt, so it
    has to be complete on its own, not a translation stub."""
    text = (text or "").strip()
    if not text:
        raise ValueError("請先輸入文章內容")

    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=1500,
        system=(
            "Extract every distinct human character mentioned or clearly implied in the "
            "given Chinese text - including characters only referred to indirectly (e.g. "
            "'the narrator', 'a mother', 'the woman with...'). For each character, give a "
            "short label (Traditional Chinese) and a concrete VISUAL description suitable "
            "as a text-to-image prompt: age, build, clothing, distinguishing features. Only "
            "include visual details actually stated or strongly implied by the text - do "
            "not invent details the text gives no basis for. Provide the visual description "
            "in both Traditional Chinese (繁體中文，never Simplified, and never mix in "
            "English words like nationality labels - write '台灣婦女' not 'Taiwanese婦女') "
            "and English - the English version is what actually drives image generation, "
            "so make it a complete, self-contained portrait-photo prompt on its own (not a "
            "literal translation fragment).\n\n"
            "This app's stories are set in Taiwan by default - unless the text clearly "
            "implies a different ethnicity/nationality for a character, the English "
            "description MUST explicitly state Taiwanese/East Asian ethnicity (e.g. "
            "'a Taiwanese woman...', 'an East Asian man...') - a text-to-image model given "
            "no ethnicity defaults to a Western-looking face, which is wrong for this app's "
            "content.\n\n"
            "Output EXACTLY a JSON array, nothing else - no markdown code fences, no "
            "commentary before or after it:\n"
            '[{"name": "...", "description_zh": "...", "description_en": "..."}, ...]'
        ),
        messages=[{"role": "user", "content": text}],
    )
    result = _JSON_FENCE_PATTERN.sub("", resp.content[0].text.strip()).strip()
    try:
        characters = json.loads(result)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Haiku 回覆不是合法 JSON：{result[:300]}") from e
    if not isinstance(characters, list):
        raise RuntimeError(f"Haiku 回覆格式不符預期（不是陣列）：{result[:300]}")
    return characters
