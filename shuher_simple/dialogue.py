"""Bounded, per-user conversation state kept in the private Google Sheet.

Do not store photos as bytes in Sheets: keep only Telegram file IDs until the
next text/voice question. Conversational history is a short, text-only summary
of the last exchange, scoped to the user's own Telegram ID.
"""
MAX_PHOTOS = 6
MAX_HISTORY = 12
MAX_HISTORY_CHARS = 10500


def new_dialogue():
    return {"history": [], "photos": [], "documents": [], "notes": ""}


def normalize_dialogue(raw):
    if not isinstance(raw, dict):
        return new_dialogue()
    state = new_dialogue()
    history = raw.get("history", [])
    if isinstance(history, list):
        for item in history[-MAX_HISTORY:]:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                continue
            text = item.get("content")
            if isinstance(text, str) and text.strip():
                state["history"].append({"role": item["role"], "content": text[:2300]})
    while sum(len(x["content"]) for x in state["history"]) > MAX_HISTORY_CHARS:
        state["history"].pop(0)
    photos = raw.get("photos", [])
    if isinstance(photos, list):
        for photo in photos[-MAX_PHOTOS:]:
            if isinstance(photo, dict) and isinstance(photo.get("file_id"), str):
                file_id = photo["file_id"].strip()
                if 0 < len(file_id) <= 320:
                    state["photos"].append({"file_id": file_id, "group": str(photo.get("group") or "")[:140],
                                            "mime": photo.get("mime") if photo.get("mime") in
                                            ("image/jpeg","image/png","image/webp") else "image/jpeg"})
    docs=raw.get("documents",[])
    if isinstance(docs,list):
        for item in docs[-4:]:
            if not isinstance(item,dict):
                continue
            file_id=str(item.get("file_id") or "")
            if not 0<len(file_id)<=320:
                continue
            state["documents"].append({
                "file_id":file_id,
                "file_name":str(item.get("file_name") or "document.pdf")[:150],
                "mime":"application/pdf",
            })
    state["notes"] = str(raw.get("notes") or "")[:1600]
    return state


def queue_photo(state, file_id, group="", caption="", mime="image/jpeg"):
    state = normalize_dialogue(state)
    if any(p["file_id"] == file_id for p in state["photos"]):
        return state, False
    if len(state["photos"]) >= MAX_PHOTOS:
        raise ValueError("Можна додати максимум 6 фото за одне обговорення. Напиши питання до них або /new.")
    was_empty = not state["photos"]
    state["photos"].append({"file_id": str(file_id), "group": str(group or "")[:140],
                            "mime": mime if mime in ("image/jpeg","image/png","image/webp") else "image/jpeg"})
    if caption:
        note = str(caption).strip()
        if note and note not in state["notes"]:
            state["notes"] = (state["notes"] + "\n" + note).strip()[:1600]
    return state, was_empty


def queue_document(state,file_id,file_name="document.pdf",caption=""):
    state=normalize_dialogue(state)
    if len(state["documents"])>=4:
        raise ValueError("Не більше чотирьох PDF за один раз.")
    if not any(item["file_id"]==file_id for item in state["documents"]):
        state["documents"].append({
            "file_id":str(file_id),"file_name":str(file_name)[:150],
            "mime":"application/pdf",
        })
    if caption:
        state["notes"]=(state["notes"]+"\n"+str(caption)).strip()[:1600]
    return state


def clear_attachments(state):
    state=normalize_dialogue(state)
    state["photos"]=[]
    state["documents"]=[]
    state["notes"]=""
    return state


def remember_exchange(state, user_text, answer, photos_used=0):
    state = normalize_dialogue(state)
    summary = str(user_text or "").strip()
    if state["notes"]:
        summary = "Підпис до фото: " + state["notes"] + "\n" + summary
    if photos_used:
        summary = f"[Надіслано {photos_used} фото] " + summary
    state["history"].append({"role": "user", "content": summary[:2300]})
    state["history"].append({"role": "assistant", "content": str(answer)[:2300]})
    state["photos"] = []
    state["notes"] = ""
    return normalize_dialogue(state)


def history_excerpt(state, max_chars=1900):
    state = normalize_dialogue(state)
    parts = [("Працівник: " if item["role"] == "user" else "Шухер: ") + item["content"]
             for item in state["history"][-6:]]
    return "\n".join(parts)[-max_chars:]
