import unittest


def unsafe_session_key(value):
    if not value:
        return False
    s = str(value)
    if ".." in s or "\\" in s:
        return True
    if s.startswith(("/", "~")):
        return True
    return len(s) >= 2 and s[0].isalpha() and s[1] == ":" and s[2:3] in ("/", "\\")


class SessionKeySlashTest(unittest.TestCase):
    def test_dingtalk_base64_keys_are_allowed(self):
        allowed = [
            "agent:main:dingtalk:dm:cidDAIsw68cJ7w/QyVwV0zB+KTziay5M9uOmLdzHoEi1tM=",
            "agent:main:dingtalk:group:cidvL9m/YqbdGp1lPYxdOOEvw==:15528999368652879",
            "agent:main:dingtalk:dm:$:LWCP_v1:$NW/8Xdhg39tkxmokXozO/Q==",
        ]
        for key in allowed:
            self.assertFalse(unsafe_session_key(key), key)

    def test_path_shaped_values_are_blocked(self):
        blocked = [
            "../../etc/passwd",
            "agent:..:x",
            "/etc/passwd",
            "~/x",
            "C:/windows/system32",
            "C:\\windows",
            "a\\b",
        ]
        for key in blocked:
            self.assertTrue(unsafe_session_key(key), key)


if __name__ == "__main__":
    unittest.main()

