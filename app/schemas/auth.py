from pydantic import BaseModel, EmailStr, Field

# Kept in step with the web client's zod schema, which requires 8 characters.
PASSWORD_MIN_LENGTH = 8


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH)


class OtpRequest(BaseModel):
    email: EmailStr


class OtpVerifyRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class MessageResponse(BaseModel):
    detail: str
