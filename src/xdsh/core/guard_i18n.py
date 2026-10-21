"""Localized guard measurements and recovery, independent of the runner."""

from xdsh.core.i18n import canonical_language, language_codes

_KEYS = ("retry_budget", "model_step_budget", "tool_budget", "repeated_tool",
         "tool_error_budget", "token_budget", "subagent_budget", "stalled", "time_budget",
         "stopped", "warning", "retained")
_ROWS = {
    "en": ("consecutive model retries", "model steps", "tool calls", "repeated identical tool", "consecutive tool failures", "cumulative tokens", "parallel subagents", "event silence", "total runtime", "Task protection stopped execution", "Nearing a task protection limit", "Prior output and progress retained; check the cause before resuming."),
    "zh-TW": ("連續模型重試", "模型步驟", "工具調用", "相同工具重複", "連續工具失敗", "累計 Token", "並行子代理", "無事件停滯", "總執行時間", "任務保護已停止執行", "接近任務保護上限", "此前答案與進度均已保留；請先核對原因再續作。"),
    "zh-CN": ("连续模型重试", "模型步骤", "工具调用", "相同工具重复", "连续工具失败", "累计 Token", "并行子代理", "无事件停滞", "总执行时间", "任务保护已停止执行", "接近任务保护上限", "此前答案与进度均已保留；请先核对原因再继续。"),
    "ja": ("モデルの連続再試行", "モデルのステップ", "ツール呼び出し", "同一ツールの反復", "ツールの連続失敗", "累積トークン", "並列サブエージェント", "イベントの停止", "合計実行時間", "タスク保護により実行を停止しました", "タスク保護の上限に近づいています", "出力と進捗は保持されています。再開前に原因を確認してください。"),
    "ko": ("연속 모델 재시도", "모델 단계", "도구 호출", "동일 도구 반복", "연속 도구 실패", "누적 토큰", "병렬 하위 에이전트", "이벤트 없음", "총 실행 시간", "작업 보호로 실행이 중지되었습니다", "작업 보호 한도에 근접했습니다", "이전 출력과 진행은 보존되었습니다. 재개 전에 원인을 확인하세요."),
    "es": ("reintentos consecutivos", "pasos del modelo", "llamadas a herramientas", "herramienta idéntica repetida", "fallos consecutivos de herramientas", "tokens acumulados", "subagentes paralelos", "sin eventos", "tiempo total", "La protección detuvo la ejecución", "Cerca del límite de protección", "Salida y progreso conservados; revisa la causa antes de continuar."),
    "fr": ("tentatives consécutives", "étapes du modèle", "appels d’outils", "outil identique répété", "échecs d’outils consécutifs", "jetons cumulés", "sous-agents parallèles", "absence d’événements", "durée totale", "La protection a arrêté l’exécution", "Limite de protection proche", "Sortie et progression conservées ; vérifiez la cause avant de reprendre."),
    "de": ("aufeinanderfolgende Modellversuche", "Modellschritte", "Werkzeugaufrufe", "identisches Werkzeug wiederholt", "aufeinanderfolgende Werkzeugfehler", "kumulierte Tokens", "parallele Unteragenten", "keine Ereignisse", "Gesamtlaufzeit", "Aufgabenschutz hat die Ausführung gestoppt", "Schutzgrenze fast erreicht", "Ausgabe und Fortschritt gespeichert; vor dem Fortsetzen Ursache prüfen."),
    "pt-BR": ("tentativas consecutivas", "etapas do modelo", "chamadas de ferramentas", "ferramenta idêntica repetida", "falhas consecutivas de ferramentas", "tokens acumulados", "subagentes paralelos", "ausência de eventos", "tempo total", "A proteção interrompeu a execução", "Perto do limite de proteção", "Saída e progresso preservados; verifique a causa antes de retomar."),
    "ru": ("повторные попытки модели", "шаги модели", "вызовы инструментов", "повтор одинакового инструмента", "последовательные сбои инструментов", "суммарные токены", "параллельные субагенты", "отсутствие событий", "общее время", "Защита остановила выполнение", "Приближение к пределу защиты", "Вывод и прогресс сохранены; проверьте причину перед продолжением."),
    "ar": ("محاولات النموذج المتتالية", "خطوات النموذج", "استدعاءات الأدوات", "تكرار الأداة نفسها", "إخفاقات الأدوات المتتالية", "الرموز المتراكمة", "الوكلاء الفرعيون المتوازون", "غياب الأحداث", "وقت التنفيذ الكلي", "أوقفت حماية المهمة التنفيذ", "الاقتراب من حد حماية المهمة", "حُفظت النتائج والتقدم؛ افحص السبب قبل الاستئناف."),
    "hi": ("लगातार मॉडल प्रयास", "मॉडल चरण", "टूल कॉल", "समान टूल का दोहराव", "लगातार टूल विफलताएँ", "कुल टोकन", "समानांतर उप-एजेंट", "कोई घटना नहीं", "कुल निष्पादन समय", "कार्य सुरक्षा ने निष्पादन रोक दिया", "कार्य सुरक्षा सीमा निकट है", "पिछला आउटपुट और प्रगति सुरक्षित हैं; जारी करने से पहले कारण जाँचें।"),
    "id": ("percobaan model berulang", "langkah model", "panggilan alat", "alat identik berulang", "kegagalan alat berturut-turut", "token kumulatif", "subagen paralel", "tanpa peristiwa", "waktu total", "Perlindungan tugas menghentikan eksekusi", "Mendekati batas perlindungan tugas", "Keluaran dan progres tersimpan; periksa penyebab sebelum melanjutkan."),
}


def guard_text(locale: str, key: str) -> str:
    try:
        index = _KEYS.index(key)
    except ValueError as error:
        raise KeyError(key) from error
    return _ROWS[canonical_language(locale)][index]


def validate_guard_catalog() -> None:
    if set(_ROWS) != set(language_codes()) or any(
        len(row) != len(_KEYS) or any(not text for text in row) for row in _ROWS.values()
    ):
        raise ValueError("guard locale coverage mismatch")
