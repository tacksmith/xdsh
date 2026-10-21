"""Fixed memory-approval text; candidate content is not translated."""

from __future__ import annotations

from string import Formatter

from xdsh.core.i18n import canonical_language, language_codes

_KEYS = ("approve", "reject", "candidate", "pending", "approved", "rejected", "error")
_ROWS = {
    "en": (
        "Approve",
        "Reject",
        "Memory candidate {id}",
        "Not applied. Only after approval will this become durable memory for this user's future tasks.",
        "Approved {id}; future tasks load it within memory budgets.",
        "Rejected {id}; content will not be applied.",
        "Could not complete the decision. Check the candidate with /learn.",
    ),
    "zh-TW": (
        "批准",
        "拒絕",
        "學習候選 {id}",
        "尚未套用；批准後才會作為同一位使用者的持久記憶加入後續任務。",
        "已批准 {id}；後續任務會按記憶預算載入。",
        "已拒絕 {id}；內容不會套用。",
        "未能完成決定；請使用 /learn 檢查候選狀態。",
    ),
    "zh-CN": (
        "批准",
        "拒绝",
        "学习候选 {id}",
        "尚未应用；批准后才会作为同一位用户的持久记忆加入后续任务。",
        "已批准 {id}；后续任务会按记忆预算加载。",
        "已拒绝 {id}；内容不会应用。",
        "未能完成决定；请使用 /learn 检查候选状态。",
    ),
    "ja": (
        "承認",
        "拒否",
        "記憶候補 {id}",
        "未適用です。承認後にのみ、このユーザーの今後のタスク用の永続記憶になります。",
        "{id} を承認しました。記憶予算内で今後のタスクに読み込みます。",
        "{id} を拒否しました。適用しません。",
        "決定を完了できませんでした。/learn で候補を確認してください。",
    ),
    "ko": (
        "승인",
        "거절",
        "기억 후보 {id}",
        "아직 적용되지 않았습니다. 승인 후에만 이 사용자의 향후 작업을 위한 영구 기억이 됩니다.",
        "{id} 승인됨. 향후 작업에서 기억 예산 내에 불러옵니다.",
        "{id} 거절됨. 적용되지 않습니다.",
        "결정을 완료하지 못했습니다. /learn으로 후보를 확인하세요.",
    ),
    "es": (
        "Aprobar",
        "Rechazar",
        "Candidato de memoria {id}",
        "No aplicado. Solo al aprobar se guardará como memoria persistente para futuras tareas de este usuario.",
        "Aprobado {id}; se cargará dentro del presupuesto de memoria.",
        "Rechazado {id}; no se aplicará.",
        "No se pudo completar la decisión. Consulta el candidato con /learn.",
    ),
    "fr": (
        "Approuver",
        "Refuser",
        "Mémoire candidate {id}",
        "Non appliquée. Après approbation seulement, elle deviendra une mémoire durable pour les futures tâches de cet utilisateur.",
        "{id} approuvé ; chargé dans les limites du budget mémoire.",
        "{id} refusé ; le contenu ne sera pas appliqué.",
        "Décision impossible à finaliser. Vérifiez le candidat avec /learn.",
    ),
    "de": (
        "Genehmigen",
        "Ablehnen",
        "Gedächtniskandidat {id}",
        "Noch nicht angewendet. Erst nach Genehmigung als dauerhaftes Gedächtnis für künftige Aufgaben dieses Nutzers verfügbar.",
        "{id} genehmigt; künftige Aufgaben laden es innerhalb des Gedächtnisbudgets.",
        "{id} abgelehnt; wird nicht angewendet.",
        "Entscheidung nicht abgeschlossen. Kandidat mit /learn prüfen.",
    ),
    "pt-BR": (
        "Aprovar",
        "Rejeitar",
        "Candidato de memória {id}",
        "Não aplicado. Somente após aprovação será memória persistente para tarefas futuras deste usuário.",
        "Aprovado {id}; carregado dentro do orçamento de memória.",
        "Rejeitado {id}; não será aplicado.",
        "Não foi possível concluir a decisão. Verifique o candidato com /learn.",
    ),
    "ru": (
        "Одобрить",
        "Отклонить",
        "Кандидат в память {id}",
        "Не применено. Только после одобрения станет постоянной памятью для будущих задач этого пользователя.",
        "{id} одобрено; загружается в пределах бюджета памяти.",
        "{id} отклонено; содержимое не будет применено.",
        "Не удалось завершить решение. Проверьте кандидата через /learn.",
    ),
    "ar": (
        "موافقة",
        "رفض",
        "ذاكرة مقترحة {id}",
        "لم تُطبّق. لن تصبح ذاكرة دائمة لمهام هذا المستخدم القادمة إلا بعد الموافقة.",
        "تمت الموافقة على {id}؛ تُحمّل ضمن ميزانية الذاكرة.",
        "تم رفض {id}؛ لن يُطبّق المحتوى.",
        "تعذر إتمام القرار. تحقّق من المقترح عبر /learn.",
    ),
    "hi": (
        "स्वीकृत करें",
        "अस्वीकार करें",
        "स्मृति उम्मीदवार {id}",
        "अभी लागू नहीं हुआ। स्वीकृति के बाद ही यह इसी उपयोगकर्ता के भविष्य के कार्यों के लिए स्थायी स्मृति बनेगा।",
        "{id} स्वीकृत; स्मृति बजट के भीतर लोड होगा।",
        "{id} अस्वीकृत; सामग्री लागू नहीं होगी।",
        "निर्णय पूरा नहीं हुआ। /learn से उम्मीदवार जाँचें।",
    ),
    "id": (
        "Setujui",
        "Tolak",
        "Kandidat memori {id}",
        "Belum diterapkan. Hanya setelah disetujui akan menjadi memori permanen untuk tugas mendatang pengguna ini.",
        "{id} disetujui; dimuat sesuai anggaran memori.",
        "{id} ditolak; isi tidak diterapkan.",
        "Keputusan belum selesai. Periksa kandidat dengan /learn.",
    ),
}


def approval_text(locale: str, key: str, **values: object) -> str:
    try:
        index = _KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _ROWS[canonical_language(locale)][index].format(**values)


def validate_approval_catalog() -> None:
    if set(_ROWS) != set(language_codes()):
        raise ValueError("approval locale coverage mismatch")
    for row in _ROWS.values():
        if len(row) != len(_KEYS):
            raise ValueError("approval key coverage mismatch")
        for message, english in zip(row, _ROWS["en"], strict=True):
            fields = {key for _, key, _, _ in Formatter().parse(message) if key is not None}
            expected = {key for _, key, _, _ in Formatter().parse(english) if key is not None}
            if not message.strip() or fields != expected:
                raise ValueError("approval placeholder mismatch")
