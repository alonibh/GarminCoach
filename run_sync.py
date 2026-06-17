import sync.sync_service as s

if __name__ == '__main__':
    s.client.login()
    print(s.run_sync())
