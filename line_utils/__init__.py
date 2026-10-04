"""LINE bot：傳股票名稱或代號給它，回股價、配息、走勢。

## 整包在什麼條件下存在

兩個環境變數缺任何一個，整支停用——路由回 503、不收訊。

    LINE_CHANNEL_SECRET        驗 webhook 簽章用
    LINE_CHANNEL_ACCESS_TOKEN  呼叫 Reply API 用

## 只用 Reply

Reply 是回應使用者傳來的那一則，**不計入免費額度**。這個 bot 所有功能都是
「問了才答」，所以用不到 Push（主動發訊，吃每月 200 則）。

## 為什麼是 5 秒

LINE 的 webhook 逾時是 5 秒，超過當失敗。所以這一條路徑上不做任何慢的事：
慢的查詢（多年歷史、全市場配息）不在 webhook 裡當場打。
"""
