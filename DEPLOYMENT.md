# Deployment Notes

## Protect Subscriber Data

Do not upload or overwrite `subscribers.db` during deployment. It contains the active subscriber list.

Preferred production setup:

```bash
mkdir -p /home/ec2-user/financial_brief_data
echo 'FINANCIAL_BRIEF_DB_PATH=/home/ec2-user/financial_brief_data/subscribers.db' >> /home/ec2-user/FinancialBrief/.env
```

If an existing production database is currently inside the code folder, move it once:

```bash
mkdir -p /home/ec2-user/financial_brief_data
mv /home/ec2-user/FinancialBrief/subscribers.db /home/ec2-user/financial_brief_data/subscribers.db
```

After that, `git pull` or code uploads cannot replace the subscriber database.

For manual `rsync` deployments, use the included `.rsync-filter`:

```bash
rsync -av --filter='merge .rsync-filter' ./ ec2-user@SERVER:/home/ec2-user/FinancialBrief/
```
