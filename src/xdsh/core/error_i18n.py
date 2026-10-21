"""Localized recovery advice without raw provider payloads or auto-replay."""

from __future__ import annotations

from xdsh.core.i18n import canonical_language, language_codes

_CODE_INDEX = {
    "TIMEOUT": 0,
    "UPSTREAM_STREAM_INCOMPLETE": 0,
    "RUNNER_STREAM_LOST": 0,
    "INVALID_REQUEST": 1,
    "ACCEPTANCE_INVALID": 2,
    "ACCEPTANCE_FAILED": 2,
}
_ROWS = {
    "en": (
        "Execution was not fully confirmed. Operations are not automatically replayed. Check /doctor before retrying or resuming.",
        "The request was rejected; this does not prove invalid credentials. Check model and reasoning settings with /doctor before resending.",
        "Explicit artifact checks failed or could not run. Model completion claims are not verified delivery.",
    ),
    "zh-TW": (
        "執行未獲完整確認；不會自動重做可能已執行的操作。請先用 /doctor 檢查，再決定重試或續作。",
        "請求被拒絕，不等於憑據失效。請用 /doctor 核對模型與推理設定，不要連續重送相同請求。",
        "明確指定的檔案驗收未通過或無法執行。不要把模型自報完成當作已驗證。",
    ),
    "zh-CN": (
        "执行未获完整确认；不会自动重做可能已执行的操作。请先用 /doctor 检查，再决定重试或续作。",
        "请求被拒绝，不等于凭据失效。请用 /doctor 核对模型与推理设置，不要连续重发相同请求。",
        "明确指定的文件验收未通过或无法执行。不要把模型自报完成当作已验证。",
    ),
    "ja": (
        "実行完了を確認できません。操作を自動で繰り返しません。再試行や再開の前に /doctor で確認してください。",
        "リクエストが拒否されましたが、認証情報が無効とは限りません。/doctor でモデルと推論設定を確認してから再送してください。",
        "指定された成果物の検証に失敗したか実行できませんでした。モデルの完了宣言は検証済みの納品を意味しません。",
    ),
    "ko": (
        "실행 완료가 확인되지 않았습니다. 작업을 자동 반복하지 않습니다. 재시도나 재개 전에 /doctor로 확인하세요.",
        "요청이 거절되었지만 인증 정보가 잘못되었다는 뜻은 아닙니다. 재전송 전에 /doctor로 모델과 추론 설정을 확인하세요.",
        "지정된 결과물 검증이 실패했거나 실행되지 않았습니다. 모델의 완료 주장은 검증된 결과를 의미하지 않습니다.",
    ),
    "es": (
        "La ejecución no se confirmó por completo. No se repiten operaciones automáticamente. Consulta /doctor antes de reintentar o continuar.",
        "Solicitud rechazada; no demuestra credenciales inválidas. Revisa el modelo y el razonamiento con /doctor antes de reenviar.",
        "Las comprobaciones de archivos fallaron o no se ejecutaron. Que el modelo declare haber terminado no verifica la entrega.",
    ),
    "fr": (
        "Exécution non entièrement confirmée. Les opérations ne sont pas répétées automatiquement. Consultez /doctor avant de réessayer ou reprendre.",
        "Requête refusée ; cela ne prouve pas que les identifiants sont invalides. Vérifiez modèle et raisonnement avec /doctor avant de renvoyer.",
        "Les vérifications des fichiers ont échoué ou n’ont pas pu s’exécuter. Une déclaration de fin du modèle ne prouve pas la livraison.",
    ),
    "de": (
        "Ausführung nicht vollständig bestätigt. Aktionen werden nicht automatisch wiederholt. Vor Wiederholung oder Fortsetzung /doctor prüfen.",
        "Anfrage abgelehnt; das beweist keine ungültigen Zugangsdaten. Modell und Schlussfolgerungseinstellungen vor erneutem Senden mit /doctor prüfen.",
        "Explizite Dateiüberprüfungen fehlgeschlagen oder nicht ausführbar. Eine Abschlussbehauptung des Modells ist kein geprüftes Ergebnis.",
    ),
    "pt-BR": (
        "Execução não totalmente confirmada. Operações não são repetidas automaticamente. Consulte /doctor antes de tentar novamente ou retomar.",
        "Solicitação rejeitada; isso não comprova credenciais inválidas. Verifique modelo e raciocínio com /doctor antes de reenviar.",
        "As verificações de arquivos falharam ou não puderam executar. A declaração de conclusão do modelo não comprova a entrega.",
    ),
    "ru": (
        "Выполнение не подтверждено полностью. Действия не повторяются автоматически. Перед повтором или продолжением проверьте /doctor.",
        "Запрос отклонён; это не доказывает неверность учётных данных. Перед повторной отправкой проверьте модель и настройки рассуждений через /doctor.",
        "Проверки файлов не пройдены или не выполнены. Заявление модели о завершении не является проверенным результатом.",
    ),
    "ar": (
        "لم يتأكد اكتمال التنفيذ. لا تُكرر الإجراءات تلقائياً. افحص /doctor قبل إعادة المحاولة أو الاستئناف.",
        "رُفض الطلب؛ لا يعني ذلك بطلان بيانات الاعتماد. افحص النموذج وإعدادات الاستدلال عبر /doctor قبل الإرسال مجدداً.",
        "فشلت فحوص الملفات المحددة أو تعذر تنفيذها. إعلان النموذج الانتهاء ليس تسليماً موثّقاً.",
    ),
    "hi": (
        "निष्पादन की पूरी पुष्टि नहीं हुई। क्रियाएँ अपने आप नहीं दोहराई जाएँगी। दोबारा प्रयास या जारी करने से पहले /doctor जाँचें।",
        "अनुरोध अस्वीकार हुआ; इससे क्रेडेंशियल गलत सिद्ध नहीं होते। दोबारा भेजने से पहले /doctor से मॉडल और तर्क सेटिंग जाँचें।",
        "निर्दिष्ट फ़ाइल जाँच विफल हुई या नहीं चल सकी। मॉडल का पूरा होने का दावा सत्यापित परिणाम नहीं है।",
    ),
    "id": (
        "Eksekusi belum terkonfirmasi penuh. Operasi tidak diulang otomatis. Periksa /doctor sebelum mencoba lagi atau melanjutkan.",
        "Permintaan ditolak; ini tidak membuktikan kredensial salah. Periksa model dan pengaturan penalaran melalui /doctor sebelum mengirim ulang.",
        "Pemeriksaan berkas gagal atau tidak dapat dijalankan. Klaim selesai dari model bukan hasil yang terverifikasi.",
    ),
}


_STREAM_IDLE = {
    "zh-CN": "模型流长时间没有新事件，并非任务总时长超限。已有进度保留；在本会话发送“继续”，先核对已有结果再续作。不会自动重跑整项任务。",
    "zh-TW": "模型流長時間沒有新事件，並非任務總時長超限。已有進度保留；在本會話發送「繼續」，先核對已有結果再續作。不會自動重跑整項任務。",
    "en": "Model stream idle, not a total task time limit. Progress is retained. Send continue in this session to check existing results and resume. No automatic task replay.",
    "ja": "モデルのイベントが途絶えました。タスク全体の時間制限ではありません。進捗は保持されます。この会話で continue を送り、既存結果を確認して再開できます。自動再実行はしません。",
    "ko": "모델 이벤트가 중단되었습니다. 전체 작업 시간 제한이 아닙니다. 진행 상황은 보존됩니다. 이 대화에 continue를 보내 기존 결과를 확인하고 재개하세요. 자동 재실행은 없습니다.",
    "es": "Flujo del modelo inactivo; no es el límite total de la tarea. Progreso conservado. Envía continue en esta sesión para comprobar resultados y reanudar. No se repite automáticamente.",
    "fr": "Flux du modèle inactif, pas une limite de durée totale. Progression conservée. Envoyez continue ici pour vérifier les résultats et reprendre. Pas de répétition automatique.",
    "de": "Modellstream inaktiv, kein Gesamtlaufzeitlimit. Fortschritt bleibt erhalten. Mit continue in dieser Sitzung Ergebnisse prüfen und fortsetzen. Keine automatische Wiederholung.",
    "pt-BR": "Fluxo do modelo inativo, não um limite de tempo total. Progresso preservado. Envie continue nesta sessão para verificar os resultados e retomar. Sem repetição automática.",
    "ru": "Поток модели неактивен; это не общий лимит времени. Прогресс сохранён. Отправьте continue в этой сессии, чтобы проверить результаты и продолжить. Автоповтора нет.",
    "ar": "توقف تدفق النموذج، وليس الحد الكلي لوقت المهمة. التقدم محفوظ. أرسل continue في هذه الجلسة للتحقق من النتائج والمتابعة. لا إعادة تلقائية للمهمة.",
    "hi": "मॉडल प्रवाह रुका है, यह कुल कार्य समय सीमा नहीं है। प्रगति सुरक्षित है। इसी सत्र में continue भेजकर मौजूदा परिणाम जाँचें और जारी रखें। स्वतः पुनः निष्पादन नहीं होगा।",
    "id": "Aliran model tidak aktif, bukan batas waktu total tugas. Kemajuan disimpan. Kirim continue dalam sesi ini untuk memeriksa hasil dan melanjutkan. Tidak ada pengulangan otomatis.",
}


def recovery_advice(locale: str, code: str) -> str:
    selected = canonical_language(locale)
    if code == "MODEL_STREAM_IDLE":
        return _STREAM_IDLE[selected]
    index = _CODE_INDEX.get(code)
    return "" if index is None else _ROWS[selected][index]


def validate_error_catalog() -> None:
    if set(_STREAM_IDLE) != set(language_codes()) or set(_ROWS) != set(language_codes()) or any(
        len(row) != 3 or any(not text for text in row) for row in _ROWS.values()
    ):
        raise ValueError("error advice locale coverage mismatch")
