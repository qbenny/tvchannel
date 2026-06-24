import requests
import re
from urllib.parse import urlparse

def login_simple(base_url, user_id, headers, authenticator, stbid, user_token,
                 stb_type, stb_version, mac, software_version, area_id,
                 user_group_id, template_name, timeout=10):
    """
    简易固定凭证登录方式
    """
    session = requests.Session()
    
    # 步骤 1: 访问 AuthenticationURL 并处理重定向 host
    url1 = f"{base_url}/EPG/jsp/AuthenticationURL?UserID={user_id}&Action=Login&FCCSupport=1"
    res1 = session.get(
        url1,
        headers={**headers, "X-Requested-With": "com.android.smart.terminal.iptv"},
        timeout=timeout
    )
    host = urlparse(res1.url).netloc
    if not host:
        host = urlparse(base_url).netloc
        
    final_base_url = f"http://{host}"
    
    # 步骤 2: 访问 authLoginHWCTC.jsp
    url2 = f"{final_base_url}/EPG/jsp/authLoginHWCTC.jsp?UserID={user_id}&SampleId="
    res2 = session.post(
        url2,
        data={"UserID": user_id, "VIP": ""},
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "Referer": url1},
        timeout=timeout
    )
    
    valid_data = {
        "UserID": user_id,
        "Lang": "1",
        "SupportHD": "1",
        "NetUserID": f"tv{user_id}@itv",
        "Authenticator": authenticator,
        "STBType": stb_type,
        "STBVersion": stb_version,
        "conntype": "4",
        "STBID": stbid,
        "templateName": template_name or "gdhdpublic",
        "areaId": area_id or "304",
        "userToken": user_token,
        "userGroupId": user_group_id or "8",
        "productPackageId": "-1",
        "mac": mac,
        "UserField": "2",
        "SoftwareVersion": software_version or stb_version,
        "IsSmartStb": "0",
        "desktopId": "",
        "stbmaker": "",
        "VIP": ""
    }
    
    url3 = f"{final_base_url}/EPG/jsp/ValidAuthenticationHWCTC.jsp"
    res3 = session.post(
        url3,
        data=valid_data,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "Referer": url2},
        timeout=timeout
    )
    
    re_token = re.search(r'UserToken\" value\=\"(.+?)\"', res3.text, re.DOTALL)
    final_token = re_token.group(1) if re_token else user_token
    
    if not final_token:
        raise ValueError("简易固定凭证登录失败，未能获取有效的 UserToken")
    
    # 提取 sessionid
    sessionid = session.cookies.get("JSESSIONID")
    if not sessionid:
        for k, v in session.cookies.get_dict().items():
            if "sessionid" in k.lower() or "session" in k.lower():
                sessionid = v
                break
    if not sessionid:
        re_sid = re.search(r'sessionid=([^"\'&>]+)', res3.text, re.IGNORECASE)
        if re_sid:
            sessionid = re_sid.group(1)

    print(f"    >>> [成功] 简易固定凭证登录成功！已获取全套通行凭证。")
    print(f"    >>> [SessionID]: {sessionid}")
    print(f"    >>> [Token]: {final_token}")
    
    return session, final_base_url, final_token
