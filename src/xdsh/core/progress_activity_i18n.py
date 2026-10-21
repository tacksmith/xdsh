"""Actual stream liveness, not guessed Todo completion."""
# ruff: noqa: E501

ROWS = {
    "en": "Live activity · {tools} tool results · {rounds} model rounds · last event {age} ago",
    "zh-CN": "实时活动 · 工具已返回 {tools} 次 · 模型 {rounds} 轮 · 最近事件 {age} 前",
    "zh-TW": "即時活動 · 工具已返回 {tools} 次 · 模型 {rounds} 輪 · 最近事件 {age} 前",
    "ja": "リアルタイム活動 · ツール結果 {tools} 件 · モデル {rounds} 回 · 最終イベント {age} 前",
    "ko": "실시간 활동 · 도구 결과 {tools}개 · 모델 {rounds}회 · 최근 이벤트 {age} 전",
    "es": "Actividad en vivo · {tools} resultados de herramientas · {rounds} rondas · último evento hace {age}",
    "fr": "Activité en direct · {tools} résultats d'outils · {rounds} tours · dernier événement il y a {age}",
    "de": "Live-Aktivität · {tools} Werkzeugergebnisse · {rounds} Modellrunden · letztes Ereignis vor {age}",
    "pt-BR": "Atividade ao vivo · {tools} resultados de ferramentas · {rounds} rodadas · último evento há {age}",
    "ru": "Текущая активность · результатов инструментов: {tools} · циклов модели: {rounds} · событие {age} назад",
    "ar": "النشاط المباشر · {tools} نتائج أدوات · {rounds} جولات نموذج · آخر حدث منذ {age}",
    "hi": "लाइव गतिविधि · {tools} टूल परिणाम · {rounds} मॉडल चक्र · पिछली घटना {age} पहले",
    "id": "Aktivitas langsung · {tools} hasil alat · {rounds} putaran model · peristiwa terakhir {age} lalu",
}


SINCE_PLAN_ROWS = {
    "en": "Live activity · Since plan update: {tools} tool results · {rounds} model rounds · last event {age} ago",
    "zh-CN": "实时活动 · 自计划更新后: 工具已返回 {tools} 次 · 模型 {rounds} 轮 · 最近事件 {age} 前",
    "zh-TW": "即時活動 · 自計畫更新後: 工具已返回 {tools} 次 · 模型 {rounds} 輪 · 最近事件 {age} 前",
    "ja": "リアルタイム活動 · 計画更新後: ツール結果 {tools} 件 · モデル {rounds} 回 · 最終イベント {age} 前",
    "ko": "실시간 활동 · 계획 갱신 후: 도구 결과 {tools}개 · 모델 {rounds}회 · 최근 이벤트 {age} 전",
    "es": "Actividad en vivo · Desde la actualización del plan: {tools} resultados de herramientas · {rounds} rondas · último evento hace {age}",
    "fr": "Activité en direct · Depuis la mise à jour du plan: {tools} résultats d'outils · {rounds} tours · dernier événement il y a {age}",
    "de": "Live-Aktivität · Seit Planaktualisierung: {tools} Werkzeugergebnisse · {rounds} Modellrunden · letztes Ereignis vor {age}",
    "pt-BR": "Atividade ao vivo · Desde a atualização do plano: {tools} resultados de ferramentas · {rounds} rodadas · último evento há {age}",
    "ru": "Текущая активность · После обновления плана: результатов инструментов {tools} · циклов модели {rounds} · событие {age} назад",
    "ar": "النشاط المباشر · منذ تحديث الخطة: {tools} نتائج أدوات · {rounds} جولات نموذج · آخر حدث منذ {age}",
    "hi": "लाइव गतिविधि · योजना अपडेट के बाद: {tools} टूल परिणाम · {rounds} मॉडल चक्र · पिछली घटना {age} पहले",
    "id": "Aktivitas langsung · Sejak pembaruan rencana: {tools} hasil alat · {rounds} putaran model · peristiwa terakhir {age} lalu",
}


def live_activity_text(
    locale: str, *, tools: int, rounds: int, age: str, since_plan: bool = False,
) -> str:
    rows = SINCE_PLAN_ROWS if since_plan else ROWS
    return rows.get(locale, rows["en"]).format(tools=tools, rounds=rounds, age=age)
