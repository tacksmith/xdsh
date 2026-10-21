"""Conservative completion-claim diagnostics, not an autonomous scientific reviewer."""
# Translation rows intentionally keep each message on one line.
# ruff: noqa: E501, RUF001
from __future__ import annotations

import re

from xdsh.core.i18n import canonical_language


def conflicting_completion_claim(answer: str) -> bool:
    """Flag explicit all-done + unresolved claims; never rewrite either claim."""
    prose = re.sub(r"(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$", "", answer[:200_000])
    prose = "\n".join(line for line in prose.splitlines()
                      if not line.startswith(("    ", "\t"))
                      and not line.lstrip().startswith(">"))
    prose = re.sub(r'`[^`\n]*`|“[^”]*”|‘[^’]*’|「[^」]*」|"[^"\n]*"', "", prose)
    claims = re.finditer(
        r"(?:已全部|全部已|均已|已经全部|已經全部)完成|"
        r"\b(?:all (?:tasks|requirements|deliverables) (?:are |have been )?complete[d]?|"
        r"fully reproduced)\b", prose[:800], re.I,
    )
    gaps = re.finditer(
        r"不可复现|不可復現|未复现|未復現|未执行|未執行|未完成|"
        r"\b(?:not (?:reproduced|executed|implemented)|unverified|unreproduced)\b",
        prose, re.I,
    )
    # These are textual diagnostics, not semantic proof. Prefer missing a
    # subtle conflict to flagging negations, quoted examples or zero gaps.
    def affirmative(match: re.Match[str]) -> bool:
        before = prose[max(0, match.start() - 18):match.start()]
        return re.search(r"(?:并非|並非|不是|没有|沒有|无|無|不|未|\bno|\bnot)\s*$",
                         before, re.I) is None

    def nonzero_gap(match: re.Match[str]) -> bool:
        after = prose[match.end():match.end() + 24]
        return affirmative(match) and not re.match(
            r"(?:项目|項目|结果|結果|项|項)?\s*[:：=]\s*(?:0(?![\d.])|无|無|none\b)",
            after, re.I,
        )

    return any(affirmative(match) for match in claims) and any(nonzero_gap(match) for match in gaps)


_MESSAGES = {
    "zh-CN": ("交付核验：回答同时声称全部完成并列出未完成项，尚不能确认全部交付。",
              "清单为模型自报进度，不等于交付验收通过。"),
    "zh-TW": ("交付核驗：回答同時聲稱全部完成並列出未完成項，尚不能確認全部交付。",
              "清單為模型自報進度，不等於交付驗收通過。"),
    "en": ("Delivery check: the answer claims completion but also lists unresolved work; full delivery is unverified.",
           "Model-reported checklist; not delivery verification."),
    "ja": ("成果物確認：完了宣言と未完了項目が併記されています。全件完了は未確認です。", "モデルの自己申告による進捗であり、成果物の検証ではありません。"),
    "ko": ("결과 확인: 완료 주장과 미완료 항목이 함께 있습니다. 전체 완료는 검증되지 않았습니다.", "모델이 보고한 진행 상황이며 결과물 검증이 아닙니다."),
    "es": ("Verificación: la respuesta declara finalización y trabajo pendiente; la entrega total no está verificada.", "Lista declarada por el modelo; no verifica la entrega."),
    "fr": ("Vérification : la réponse annonce la fin mais aussi des travaux non résolus ; la livraison complète reste non vérifiée.", "Liste déclarée par le modèle, pas une vérification des livrables."),
    "de": ("Ergebnisprüfung: Die Antwort meldet Abschluss und zugleich offene Arbeiten; vollständige Lieferung ist nicht bestätigt.", "Vom Modell gemeldete Checkliste, keine Ergebnisprüfung."),
    "pt-BR": ("Verificação: a resposta declara conclusão e também pendências; a entrega completa não foi verificada.", "Lista informada pelo modelo; não é verificação da entrega."),
    "ru": ("Проверка: ответ заявляет о завершении, но перечисляет нерешённые задачи; полнота результата не подтверждена.", "Список со слов модели, не проверка результата."),
    "ar": ("فحص التسليم: يعلن الرد الاكتمال ويذكر أعمالاً غير منجزة؛ لم يُتحقق من اكتمال التسليم.", "قائمة أبلغ عنها النموذج وليست تحققاً من التسليم."),
    "hi": ("परिणाम जाँच: उत्तर पूर्णता का दावा करता है पर लंबित काम भी बताता है; पूर्ण परिणाम सत्यापित नहीं है।", "यह मॉडल द्वारा बताई गई सूची है, परिणाम का सत्यापन नहीं।"),
    "id": ("Pemeriksaan: jawaban mengklaim selesai tetapi mencantumkan pekerjaan tersisa; hasil lengkap belum terverifikasi.", "Daftar yang dilaporkan model, bukan verifikasi hasil."),
}


def completion_note(language: str, *, conflict: bool = False) -> str:
    return _MESSAGES[canonical_language(language)][0 if conflict else 1]
