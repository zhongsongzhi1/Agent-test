from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from agentops_assessment.backend import database


def _decode_user(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "roles": database.decode_json(row["roles_json"], []),
        "permissions": database.decode_json(row["permissions_json"], []),
    }


def get_user(user_id: str) -> dict | None:
    with database.connect() as conn:
        database.init_db(conn)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _decode_user(row) if row else None


def get_current_user(x_user_id: Annotated[str | None, Header()] = None) -> dict:
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-User-Id 请求头。",
        )
    user = get_user(x_user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"未知用户: {x_user_id}",
        )
    return user


def require_permissions(*permissions: str):
    def dependency(user: dict = Depends(get_current_user)) -> dict:
        missing = [p for p in permissions if p not in user["permissions"]]
        if missing:
            # P1: 在权限拒绝路径写入审计日志
            try:
                with database.connect() as conn:
                    database.init_db(conn)
                    database.insert_audit_log(
                        conn,
                        actor_id=user.get("id", "unknown"),
                        action="permission.denied",
                        resource="permission_check",
                        payload={"missing_permissions": missing},
                        decision="deny",
                    )
            except Exception:
                # 不应因日志写入失败而改变原始行为；吞掉并继续抛出权限拒绝
                pass
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"missing_permissions": missing},
            )
        return user

    return dependency
