#!/usr/bin/env python3
"""
SSL Certificate Generator for AoiTalk

Generates self-signed SSL certificates for HTTPS support using Python's cryptography library.
Run this script once to create certificates in the certs/ directory.

Usage:
    python scripts/generate_ssl_cert.py
    python scripts/generate_ssl_cert.py --days 730  # 2 years validity
    python scripts/generate_ssl_cert.py --ip 192.168.1.100  # Add custom IP to SAN
"""

import sys
import os
from pathlib import Path
import argparse
from datetime import datetime, timedelta, timezone

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    import ipaddress
except ImportError:
    print("❌ cryptography library not found!")
    print("   Install it with: pip install cryptography")
    sys.exit(1)


def generate_certificate(
    output_dir: Path,
    days: int = 365,
    common_name: str = "AoiTalk",
    additional_ips: list[str] | None = None
) -> bool:
    """
    Generate self-signed SSL certificate using Python's cryptography library.
    
    Args:
        output_dir: Directory to save certificate files
        days: Certificate validity in days
        common_name: Common Name for the certificate
        additional_ips: Additional IP addresses to add to SAN
        
    Returns:
        True if successful, False otherwise
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    key_file = output_dir / "server.key"
    cert_file = output_dir / "server.crt"
    
    print(f"🔐 Generating SSL certificate...")
    print(f"   Output: {output_dir}")
    print(f"   Validity: {days} days")
    
    try:
        # Generate RSA private key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=4096,
            backend=default_backend()
        )
        
        # Build Subject Alternative Names
        san_entries = [
            x509.DNSName("localhost"),
            x509.DNSName("*.localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address("::1")),
        ]
        
        # Add custom IPs
        if additional_ips:
            for ip in additional_ips:
                try:
                    san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
                    print(f"   Added custom IP: {ip}")
                except ValueError:
                    print(f"   ⚠️ Invalid IP address: {ip}")
        
        # Try to detect local IP
        try:
            import socket
            local_ip = socket.gethostbyname(socket.gethostname())
            if local_ip and local_ip not in ["127.0.0.1", "::1"]:
                san_entries.append(x509.IPAddress(ipaddress.ip_address(local_ip)))
                print(f"📡 Detected local IP: {local_ip}")
        except Exception:
            pass
        
        # Build subject
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "JP"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AoiTalk"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        
        # Build certificate
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=days))
            .add_extension(
                x509.SubjectAlternativeName(san_entries),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .sign(private_key, hashes.SHA256(), default_backend())
        )
        
        # Write private key
        with open(key_file, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))
        
        # Write certificate
        with open(cert_file, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        
        print(f"\n✅ Certificate generated successfully!")
        print(f"   🔑 Key file:  {key_file}")
        print(f"   📜 Cert file: {cert_file}")
        print(f"\n⚠️  Note: This is a self-signed certificate.")
        print(f"   Browsers will show a security warning.")
        print(f"   For production, consider using Let's Encrypt.")
        return True
        
    except Exception as e:
        print(f"❌ Certificate generation error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Generate self-signed SSL certificates for AoiTalk"
    )
    parser.add_argument(
        "--days", type=int, default=365,
        help="Certificate validity in days (default: 365)"
    )
    parser.add_argument(
        "--ip", action="append", dest="ips",
        help="Additional IP address to add to SAN (can be used multiple times)"
    )
    parser.add_argument(
        "--output", type=str, default="certs",
        help="Output directory (default: certs)"
    )
    
    args = parser.parse_args()
    
    # Get project root (parent of scripts/)
    project_root = Path(__file__).parent.parent
    output_dir = project_root / args.output
    
    success = generate_certificate(
        output_dir=output_dir,
        days=args.days,
        additional_ips=args.ips
    )
    
    if success:
        print(f"\n📝 Next steps:")
        print(f"   1. Verify .env has:")
        print(f"      AOITALK_SSL_ENABLED=true")
        print(f"      AOITALK_SSL_KEYFILE={args.output}/server.key")
        print(f"      AOITALK_SSL_CERTFILE={args.output}/server.crt")
        print(f"   2. Restart AoiTalk")
        print(f"   3. Access via https://127.0.0.1:3000")
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
